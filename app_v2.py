from __future__ import annotations

import difflib
import gc
import ctypes
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from array import array
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import (
    QEvent, QItemSelectionModel, QObject, QPointF, QThread, QTimer, Qt, QUrl, Signal,
)
from PySide6.QtGui import (
    QBrush, QColor, QCursor, QFont, QIcon, QKeySequence, QLinearGradient, QPainter,
    QPen, QPixmap, QPolygonF, QShortcut,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QFontComboBox, QGraphicsItem, QGraphicsScene, QGraphicsTextItem, QGraphicsView,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollBar, QSizePolicy, QSlider, QSpinBox, QSplitter, QStyledItemDelegate,
    QTableWidget, QTableWidgetItem, QTabWidget, QToolTip, QVBoxLayout, QWidget,
)


APP_NAME = "Din Subtitler"
FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
MODELS_DIR = ROOT / "models"
BIN_DIR = BUNDLE_ROOT if FROZEN else ROOT / "bin"
CRASH_LOG = ROOT / "crash.log"
VIDEO_TYPES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpeg", ".mpg", ".wmv"}
os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")
_DLL_HANDLES = []


def activate_gpu_dlls():
    if os.name != "nt":
        return
    for pkg in ("cublas", "cudnn"):
        dll_dir = (
            BUNDLE_ROOT / "nvidia" / pkg / "bin"
            if FROZEN else
            Path(sys.prefix) / "Lib" / "site-packages" / "nvidia" / pkg / "bin"
        )
        if dll_dir.exists():
            os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ["PATH"]
            if str(dll_dir) not in getattr(activate_gpu_dlls, "paths", set()):
                _DLL_HANDLES.append(os.add_dll_directory(str(dll_dir)))
                activate_gpu_dlls.paths = getattr(activate_gpu_dlls, "paths", set())
                activate_gpu_dlls.paths.add(str(dll_dir))


activate_gpu_dlls()


def _log_uncaught(exc_type, exc_value, exc_tb):
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    sys.stderr.write(text)
    try:
        with CRASH_LOG.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


sys.excepthook = _log_uncaught


def srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def clock(ms: int) -> str:
    sec = max(0, ms // 1000)
    return f"{sec // 60:02}:{sec % 60:02}"


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_lines(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def editor_time(seconds: float) -> str:
    return srt_time(seconds).replace(",", ".")


def read_srt(path: Path) -> list[dict]:
    raw = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            raw = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        raise RuntimeError("Não consegui identificar a codificação desse SRT.")

    def seconds(value: str) -> float:
        match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})", value.strip())
        if not match:
            raise ValueError(value)
        h, m, s, ms = map(int, match.groups())
        return h * 3600 + m * 60 + s + ms / 1000

    captions = []
    for block in re.split(r"\r?\n\s*\r?\n", raw.strip()):
        lines = block.splitlines()
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        try:
            start_raw, end_raw = lines[timing_index].split("-->", 1)
            text = clean_lines("\n".join(lines[timing_index + 1:]))
            if text:
                captions.append({
                    "start": seconds(start_raw),
                    "end": seconds(end_raw),
                    "text": text,
                })
        except (ValueError, IndexError):
            continue
    if not captions:
        raise RuntimeError("Nenhum trecho de legenda válido foi encontrado nesse arquivo.")
    return sorted(captions, key=lambda item: (item["start"], item["end"]))


def wrap_caption(text: str, limit: int = 42) -> str:
    text = clean_lines(text)
    if "\n" in text:
        return text
    if len(text) <= limit:
        return text
    words = text.split()
    if len(words) < 2:
        return text
    best = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        fits = len(a) <= limit and len(b) <= limit
        marker = words[i - 1].rstrip("\"'”)")
        bonus = -8 if marker.endswith((",", ";", ":", ".", "!", "?", "…", "—")) else 0
        score = (0 if fits else 1, abs(len(a) - len(b)) + bonus)
        if best is None or score < best[0]:
            best = (score, a, b)
    return f"{best[1]}\n{best[2]}"


CAPTION_MAX_CHARS = 80
CAPTION_MIN_CHARS = 12
CAPTION_PAUSE_SECONDS = 0.45


def split_words_into_captions(words, max_chars: int = CAPTION_MAX_CHARS) -> list[dict]:
    captions = []
    start = 0
    length = 0
    comma_at = None
    comma_length = 0

    def flush(end):
        nonlocal start
        chunk = words[start:end]
        text = clean("".join(w.word for w in chunk))
        if text:
            captions.append({"start": float(chunk[0].start), "end": float(chunk[-1].end), "text": text})
        start = end

    for i, w in enumerate(words):
        length += len(w.word)
        is_last = i == len(words) - 1
        stripped = w.word.strip()
        if stripped.endswith((",", ";", ":")):
            comma_at, comma_length = i, length
        ends_sentence = stripped.endswith((".", "!", "?", "…"))
        gap = words[i + 1].start - w.end if not is_last else 0.0
        if is_last or ends_sentence:
            flush(i + 1)
            comma_at, comma_length, length = None, 0, 0
        elif gap >= CAPTION_PAUSE_SECONDS and length >= CAPTION_MIN_CHARS:
            flush(i + 1)
            comma_at, comma_length, length = None, 0, 0
        elif length >= max_chars:
            if comma_at is not None:
                flush(comma_at + 1)
                length -= comma_length
            else:
                flush(i + 1)
                length = 0
            comma_at, comma_length = None, 0
    return captions


EN_CAPTION_MAX_CHARS = 84


def write_srt(path: Path, captions: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="\n") as f:
        for i, c in enumerate(captions, 1):
            f.write(f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n")
            f.write(f"{wrap_caption(c['text'])}\n\n")


def fetch_lyrics(artist: str, track: str) -> str:
    query = urllib.parse.urlencode({"artist_name": artist, "track_name": track})
    request = urllib.request.Request(
        f"https://lrclib.net/api/search?{query}",
        headers={"User-Agent": f"{APP_NAME} (https://github.com/randomicidio/DinSubtitler)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            results = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"Não consegui consultar a letra na internet.\n{exc}") from exc
    for item in results:
        lyrics = (item.get("plainLyrics") or "").strip()
        if lyrics:
            return lyrics
    raise RuntimeError(
        "Nenhuma letra foi encontrada para essa música.\n"
        "Confira o nome do artista e da música, ou cole a letra manualmente."
    )


def lyric_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = clean(raw)
        if not line or re.fullmatch(r"[\[(].*[\])]", line):
            continue
        lines.append(line)
    return lines


def normalize_token(word: str) -> str:
    word = unicodedata.normalize("NFD", word.lower())
    return "".join(c for c in word if c.isalnum() and not unicodedata.combining(c))


WORD_MAX_HOLD_SECONDS = 3.5


def mark_isolated_words(words: list[dict], gap_seconds: float = 0.5) -> None:
    """Sinaliza palavras precedidas por silêncio, de qualquer origem.

    Uma nota segurada logo após uma pausa (fim de frase anterior, respiro,
    trecho instrumental) tende a ter o início esticado pelo Whisper; isso
    é comum o bastante para não depender de ser a primeira palavra do
    segmento interno do Whisper.
    """
    previous_end = 0.0
    for word in words:
        word["isolated_start"] = (word["start"] - previous_end) > gap_seconds
        previous_end = word["end"]


_similarity_cache: dict = {}


def token_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    key = (a, b) if a <= b else (b, a)
    value = _similarity_cache.get(key)
    if value is None:
        value = difflib.SequenceMatcher(None, a, b).ratio()
        _similarity_cache[key] = value
    return value


ALIGN_MATCH_MIN = 0.55
ALIGN_GAP = -0.18


def match_lyric_tokens(line_tokens: list, heard: list[str]) -> list[tuple[int, int, float]]:
    """Alinhamento global fuzzy entre a letra e o que o Whisper ouviu.

    Programação dinâmica semi-global (pontas livres): a sequência ouvida
    encontra seu melhor encaixe dentro da letra inteira, com crédito
    parcial para palavras parecidas — uma palavra mal ouvida não perde a
    âncora, e um refrão repetido cai na repetição certa porque o encaixe
    é decidido pela música como um todo, não bloco a bloco.
    Retorna (índice na letra, índice ouvido, similaridade) dos casamentos.
    """
    n, m = len(line_tokens), len(heard)
    if not n or not m:
        return []
    scores = [[0.0] * (m + 1) for _ in range(n + 1)]
    moves = [[0] * (m + 1) for _ in range(n + 1)]  # 1=casa, 2=pula letra, 3=pula ouvido
    for i in range(1, n + 1):
        token = line_tokens[i - 1][0]
        row, prev_row = scores[i], scores[i - 1]
        move_row = moves[i]
        for j in range(1, m + 1):
            sim = token_similarity(token, heard[j - 1])
            gain = sim if sim >= ALIGN_MATCH_MIN else -0.4
            best, move = prev_row[j - 1] + gain, 1
            up = prev_row[j] + ALIGN_GAP
            if up > best:
                best, move = up, 2
            left = row[j - 1] + ALIGN_GAP
            if left > best:
                best, move = left, 3
            if best < 0.0:
                best, move = 0.0, 0
            row[j] = best
            move_row[j] = move
    # Alinhamento local: o melhor encaixe pode terminar em qualquer célula.
    i, j, best = 0, 0, 0.0
    for row_index in range(1, n + 1):
        row = scores[row_index]
        for col_index in range(1, m + 1):
            if row[col_index] > best:
                i, j, best = row_index, col_index, row[col_index]
    matches = []
    while i > 0 and j > 0 and moves[i][j]:
        move = moves[i][j]
        if move == 1:
            sim = token_similarity(line_tokens[i - 1][0], heard[j - 1])
            if sim >= ALIGN_MATCH_MIN:
                matches.append((i - 1, j - 1, sim))
            i, j = i - 1, j - 1
        elif move == 2:
            i -= 1
        else:
            j -= 1
    matches.reverse()
    return matches


def align_lyrics_to_words(lines: list[str], words: list[dict]) -> list[dict]:
    line_tokens = []
    token_counts = [0] * len(lines)
    for index, line in enumerate(lines):
        for token in (normalize_token(w) for w in line.split()):
            if token:
                line_tokens.append((token, index))
                token_counts[index] += 1
    kept = [(normalize_token(w["word"]), i) for i, w in enumerate(words)]
    kept = [(token, i) for token, i in kept if token]
    heard = [token for token, _ in kept]
    spans: list[list[float] | None] = [None] * len(lines)
    matched_weights = [0.0] * len(lines)
    for a, b, sim in match_lyric_tokens(line_tokens, heard):
        _, line_index = line_tokens[a]
        word = words[kept[b][1]]
        matched_weights[line_index] += sim
        raw_start, raw_end = word["start"], word["end"]
        # Depois de um silêncio, o Whisper costuma esticar a palavra para
        # trás, cobrindo o instrumental; sem o limite, o verso apareceria
        # cedo demais e ainda engoliria o espaço do verso anterior.
        back_limit = 1.0 if word.get("isolated_start") else 3.0
        start = max(raw_start, raw_end - back_limit)
        # Uma nota segurada (melisma) pode fazer o Whisper grudar tempo
        # demais numa única palavra; sem o teto, ela invade o verso
        # seguinte em vez de só esticar o próprio verso.
        end = min(raw_end, raw_start + WORD_MAX_HOLD_SECONDS)
        span = spans[line_index]
        if span is None:
            spans[line_index] = [start, end]
        else:
            span[0] = min(span[0], start)
            span[1] = max(span[1], end)
    # Uma palavra solta parecida não prova que o verso foi cantado ali: só
    # vale como âncora o verso com peso somado de boa parte das palavras.
    for index, span in enumerate(spans):
        if span is None:
            continue
        required = max(0.9, token_counts[index] * 0.35)
        if matched_weights[index] < required:
            spans[index] = None
    return spans


LYRIC_MIN_LINE_SECONDS = 0.4


def syllable_weight(line: str) -> int:
    plain = unicodedata.normalize("NFD", line.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return max(1, len(re.findall(r"[aeiouy]+", plain)))


def envelope_thresholds(env):
    """Piso e nível de "som ativo" relativos ao contraste real da faixa."""
    import numpy as np

    quiet = float(np.percentile(env, 20))
    peak = float(np.percentile(env, 95))
    if peak <= quiet + 1e-4:
        return None
    return quiet, quiet + (peak - quiet) * 0.3


def voiced_window(env, begin: float, finish: float):
    """Encontra do primeiro ao último instante com som ativo no intervalo."""
    import numpy as np

    if env is None or not len(env):
        return None
    hop = LYRIC_ENVELOPE_HOP
    a = max(0, int(begin / hop))
    b = min(len(env), int(finish / hop))
    if b - a < 5:
        return None
    thresholds = envelope_thresholds(env)
    if not thresholds:
        return None
    _, loud = thresholds
    active = np.flatnonzero(env[a:b] >= loud)
    if not len(active):
        return None
    v0 = float((a + active[0]) * hop)
    v1 = float((a + active[-1] + 1) * hop)
    if v1 - v0 < 0.3:
        return None
    return v0, v1


def captions_from_lyrics(lines: list[str], spans: list, total: float, env=None) -> list[dict]:
    if not any(spans):
        raise RuntimeError(
            "Não consegui reconhecer a letra dessa música no áudio.\n"
            "Confira se a letra corresponde ao que é cantado no vídeo."
        )
    # O vídeo pode mostrar só um trecho da música: versos antes do primeiro
    # ponto reconhecido e depois do último são descartados, não inventados.
    anchors = [i for i, span in enumerate(spans) if span]
    filled = {i: list(spans[i]) for i in anchors}

    def distribute(indexes, begin, finish):
        # No canto, o tempo acompanha as sílabas (cada uma pode virar uma
        # nota), então versos com mais sílabas ganham fatias maiores.
        weights = [syllable_weight(lines[i]) for i in indexes]
        scale = (finish - begin) / sum(weights)
        cursor = begin
        for i, weight in zip(indexes, weights):
            filled[i] = [cursor, cursor + weight * scale]
            cursor += weight * scale

    for prev, nxt in zip(anchors, anchors[1:]):
        missing = nxt - prev - 1
        if not missing:
            continue
        begin, finish = filled[prev][1], filled[nxt][0]
        gap = finish - begin
        # Mesmo sem reconhecer as palavras, a presença de voz no intervalo
        # diz onde os versos que faltam foram cantados.
        window = voiced_window(env, begin, finish)
        if window:
            distribute(range(prev + 1, nxt), *window)
            continue
        if gap / missing < LYRIC_MIN_LINE_SECONDS:
            if missing > 2:
                # Muitos versos seguidos sem espaço nenhum: a seção foi
                # pulada no vídeo, então não são inventados tempos para ela.
                continue
            # Poucos versos faltando é dúvida de reconhecimento, não corte:
            # a sequência da letra tem prioridade, roubando tempo dos
            # vizinhos (que encolhem até um mínimo de 0,6s) se necessário.
            deficit = missing * 1.2 - gap
            spare_prev = max(0.0, (filled[prev][1] - filled[prev][0]) - 0.6)
            spare_next = max(0.0, (filled[nxt][1] - filled[nxt][0]) - 0.6)
            take_prev = min(spare_prev, deficit / 2)
            take_next = min(spare_next, deficit - take_prev)
            take_prev = min(spare_prev, deficit - take_next)
            filled[prev][1] -= take_prev
            filled[nxt][0] += take_next
            begin, finish = filled[prev][1], filled[nxt][0]
            gap = max(finish - begin, missing * 0.6)
        distribute(range(prev + 1, nxt), begin, begin + gap)

    # Versos antes da primeira âncora (e depois da última) normalmente não
    # estão no vídeo — mas se ali existe voz cantada, eles são resgatados,
    # tantos quantos couberem no trecho com voz.
    first = anchors[0]
    if first > 0:
        window = voiced_window(env, max(0.0, filled[first][0] - 20.0), filled[first][0] - 0.05)
        if window:
            count = min(first, max(1, int((window[1] - window[0]) / 1.5)))
            distribute(range(first - count, first), *window)
    last = anchors[-1]
    if last < len(lines) - 1:
        window = voiced_window(env, filled[last][1] + 0.05, min(total, filled[last][1] + 20.0))
        if window:
            count = min(len(lines) - 1 - last, max(1, int((window[1] - window[0]) / 1.5)))
            distribute(range(last + 1, last + 1 + count), *window)
    captions = []
    for index in sorted(filled):
        start, end = filled[index]
        if captions and start < captions[-1]["end"]:
            start = captions[-1]["end"]
        end = min(max(end, start + 0.4), max(total, start + 0.4))
        captions.append({
            "start": round(float(start), 3), "end": round(float(end), 3), "text": lines[index],
        })
    return captions


LYRIC_ENVELOPE_HOP = 0.01


def audio_envelope(audio_path: Path, emphasize_highs: bool = True):
    import numpy as np
    import wave

    with wave.open(str(audio_path), "rb") as wav:
        rate = wav.getframerate()
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    samples = data.astype(np.float32) / 32768.0
    if emphasize_highs:
        # A diferença entre amostras realça os agudos, reduzindo o peso de
        # bumbo e baixo: o envelope fica mais próximo da presença da voz.
        samples = np.diff(samples, prepend=samples[:1])
    step = max(1, int(rate * LYRIC_ENVELOPE_HOP))
    count = len(samples) // step
    if not count:
        return None
    frames = samples[: count * step].reshape(count, step)
    env = np.sqrt((frames ** 2).mean(axis=1))
    return np.convolve(env, np.ones(5) / 5, mode="same")


VOCALS_MODEL_URL = (
    "https://github.com/TRvlvr/model_repo/releases/download/"
    "all_public_uvr_models/Kim_Vocal_2.onnx"
)
VOCALS_MODEL_PATH = MODELS_DIR / "vocals" / "Kim_Vocal_2.onnx"
MDX_N_FFT = 7680
MDX_HOP = 1024
MDX_DIM_F = 3072
MDX_DIM_T = 256
MDX_COMPENSATE = 1.009
MDX_TRIM = MDX_N_FFT // 2
MDX_CHUNK = MDX_HOP * (MDX_DIM_T - 1)
MDX_GEN = MDX_CHUNK - 2 * MDX_TRIM


def ensure_vocals_model(emit) -> bool:
    if VOCALS_MODEL_PATH.is_file():
        return True
    try:
        VOCALS_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            VOCALS_MODEL_URL, headers={"User-Agent": APP_NAME}
        )
        partial = VOCALS_MODEL_PATH.with_suffix(".part")
        with urllib.request.urlopen(request, timeout=30) as response, partial.open("wb") as f:
            length = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                block = response.read(1 << 18)
                if not block:
                    break
                f.write(block)
                done += len(block)
                pct = f" {done * 100 // length}%" if length else ""
                emit(f"Baixando o modelo de separação de voz…{pct}", 8)
        partial.replace(VOCALS_MODEL_PATH)
        return True
    except Exception:
        return False


def _mdx_window():
    import numpy as np

    return 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(MDX_N_FFT) / MDX_N_FFT)


def _mdx_stft(chunk, window):
    import numpy as np

    padded = np.pad(chunk, ((0, 0), (MDX_TRIM, MDX_TRIM)), mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(
        padded, MDX_N_FFT, axis=1
    )[:, ::MDX_HOP]
    return np.fft.rfft(frames * window, axis=2).transpose(0, 2, 1)


def _mdx_istft(spec, window):
    import numpy as np

    frames = np.fft.irfft(spec.transpose(0, 2, 1), n=MDX_N_FFT, axis=2) * window
    out = np.zeros((2, MDX_CHUNK + MDX_N_FFT))
    norm = np.zeros(MDX_CHUNK + MDX_N_FFT)
    squared = window * window
    for t in range(frames.shape[1]):
        pos = t * MDX_HOP
        out[:, pos:pos + MDX_N_FFT] += frames[:, t]
        norm[pos:pos + MDX_N_FFT] += squared
    norm[norm < 1e-10] = 1.0
    return (out / norm)[:, MDX_TRIM:MDX_TRIM + MDX_CHUNK]


def separate_vocals_file(mix_path: Path, vocals_path: Path, progress) -> None:
    """Isola a voz de um WAV estéreo 44.1kHz com o modelo MDX-Net.

    As janelas avançam com sobreposição parcial (menos redundância que
    50%, mas ainda o suficiente para evitar emendas audíveis) e o
    resultado nas áreas sobrepostas é uma média.
    """
    import numpy as np
    import onnxruntime
    import wave

    with wave.open(str(mix_path), "rb") as wav:
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    mix = (data.reshape(-1, 2).T / 32768.0).astype(np.float32)
    length = mix.shape[1]
    options = onnxruntime.SessionOptions()
    # Deixa pelo menos um núcleo livre: usar todos os núcleos aqui pode
    # sufocar a thread da interface gráfica e a decodificação de vídeo o
    # suficiente para desestabilizar o Qt durante o processamento.
    options.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
    session = onnxruntime.InferenceSession(
        str(VOCALS_MODEL_PATH), sess_options=options, providers=["CPUExecutionProvider"]
    )
    window = _mdx_window()

    def infer(piece):
        spec = _mdx_stft(piece, window)[:, :MDX_DIM_F, :]
        tensor = np.stack(
            [spec[0].real, spec[0].imag, spec[1].real, spec[1].imag]
        ).astype(np.float32)[None]
        out = session.run(None, {"input": tensor})[0][0]
        full = np.zeros((2, MDX_N_FFT // 2 + 1, MDX_DIM_T), np.complex128)
        full[0, :MDX_DIM_F] = out[0] + 1j * out[1]
        full[1, :MDX_DIM_F] = out[2] + 1j * out[3]
        return _mdx_istft(full, window)

    pad = MDX_GEN - (length % MDX_GEN or MDX_GEN)
    padded = np.concatenate(
        [np.zeros((2, MDX_TRIM), np.float32), mix,
         np.zeros((2, pad + MDX_TRIM), np.float32)], axis=1,
    )
    step = MDX_GEN * 2 // 3
    starts = list(range(0, padded.shape[1] - MDX_CHUNK + 1, step))
    total_samples = padded.shape[1]
    acc = np.zeros((2, total_samples))
    hits = np.zeros(total_samples)
    for index, position in enumerate(starts):
        piece = padded[:, position:position + MDX_CHUNK]
        clean = infer(piece)
        acc[:, position + MDX_TRIM:position + MDX_TRIM + MDX_GEN] += (
            clean[:, MDX_TRIM:MDX_TRIM + MDX_GEN]
        )
        hits[position + MDX_TRIM:position + MDX_TRIM + MDX_GEN] += 1
        progress(index + 1, len(starts))
    hits[hits < 1] = 1
    vocals = (acc / hits)[:, MDX_TRIM:MDX_TRIM + length] * MDX_COMPENSATE
    # Normaliza o volume: a voz separada costuma sair baixa, o que
    # prejudica o reconhecimento do Whisper.
    peak = float(np.abs(vocals).max())
    if peak > 1e-4:
        vocals = vocals * min(0.9 / peak, 10.0)
    samples = (np.clip(vocals, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(vocals_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(samples.T.reshape(-1).tobytes())


def merge_spans(primary: list, secondary: list) -> list:
    """Completa as âncoras da voz isolada com as do mix que respeitam a ordem."""
    merged = [list(span) if span else None for span in primary]
    for index, span in enumerate(secondary):
        if merged[index] is not None or span is None:
            continue
        prev_end = next(
            (merged[j][1] for j in range(index - 1, -1, -1) if merged[j]), 0.0
        )
        next_start = next(
            (merged[j][0] for j in range(index + 1, len(merged)) if merged[j]), None
        )
        if span[0] >= prev_end - 0.3 and (next_start is None or span[1] <= next_start + 0.3):
            merged[index] = list(span)
    return merged


def refine_lyric_starts(captions: list[dict], env) -> list[dict]:
    """Empurra para a frente versos que "começam" em região quieta do áudio.

    O Whisper tende a adiantar o início dos versos cantados; quando o som
    naquele instante ainda está baixo, o início é movido para a primeira
    subida sustentada de energia. Nunca move para trás.
    """
    import numpy as np

    if env is None or not len(env):
        return captions
    hop = LYRIC_ENVELOPE_HOP
    thresholds = envelope_thresholds(env)
    if not thresholds:
        return captions
    _, loud = thresholds
    for index, cap in enumerate(captions):
        start, end = cap["start"], cap["end"]
        horizon = min(start + 2.5, end - 0.2)
        w0, w1 = int(max(0.0, start) / hop), int(horizon / hop)
        if w1 <= w0 or w0 >= len(env):
            continue
        if env[min(w0, len(env) - 1)] >= loud * 0.8:
            continue
        onset = None
        run = 0
        for k in range(w0, min(w1, len(env))):
            run = run + 1 if env[k] >= loud else 0
            if run >= 8:
                onset = (k - run + 1) * hop
                break
        if onset is not None and onset > start + 0.15:
            floor = captions[index - 1]["end"] if index else 0.0
            cap["start"] = round(min(max(floor, onset - 0.1), end - 0.3), 3)
    return captions


def voiced_run_end(env, begin: float, limit: float) -> float:
    """Até onde a voz continua soando a partir de `begin` (tolera respiros curtos)."""
    if env is None or not len(env):
        return begin
    thresholds = envelope_thresholds(env)
    if not thresholds:
        return begin
    _, loud = thresholds
    hop = LYRIC_ENVELOPE_HOP
    position = int(begin / hop)
    stop = min(int(limit / hop), len(env))
    silent_run, last_voiced = 0, position
    while position < stop:
        if env[position] >= loud:
            silent_run = 0
            last_voiced = position
        else:
            silent_run += 1
            if silent_run > 30:  # 0,3s de silêncio encerra a nota
                break
        position += 1
    return max(begin, last_voiced * hop)


def extend_lyric_ends(captions: list[dict], total: float, env=None) -> list[dict]:
    # Notas sustentadas duram mais do que o Whisper marca: cada verso
    # permanece na tela até o próximo começar (estilo karaokê) e, em
    # pausas maiores, segue o envelope enquanto a voz continuar soando.
    for current, following in zip(captions, captions[1:]):
        gap = following["start"] - current["end"]
        if 0 < gap <= 2.0:
            current["end"] = following["start"]
        elif gap > 2.0:
            held = voiced_run_end(env, current["end"], following["start"] - 0.2)
            current["end"] = round(max(current["end"] + 0.8, held), 3)
    if captions:
        last = captions[-1]
        held = voiced_run_end(env, last["end"], max(total, last["end"]))
        last["end"] = round(min(max(last["end"] + 1.2, held), max(total, last["end"])), 3)
    return captions


class Engine:
    def __init__(self, progress):
        self.progress = progress

    def emit(self, text, value):
        self.progress(text, value)

    def ensure_tools(self):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg não foi encontrado.")

    def _run_whisper(self, video: Path, task: str, label: str, max_chars: int) -> list[dict]:
        self.ensure_tools()
        with tempfile.TemporaryDirectory(prefix="din-subtitler-") as td:
            audio = Path(td) / "audio.wav"
            self.emit("Extraindo áudio…", 8)
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", str(audio)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode:
                raise RuntimeError("Não consegui extrair o áudio desse vídeo.")
            from faster_whisper import WhisperModel

            def run(device, compute):
                model = WhisperModel(str(MODELS_DIR / "whisper"), device=device, compute_type=compute)
                segs, info = model.transcribe(
                    str(audio), language="pt", task=task, vad_filter=True,
                    beam_size=5, condition_on_previous_text=True, word_timestamps=True,
                )
                total = max(info.duration, 0.001)
                captions = []
                for s in segs:
                    if s.words:
                        captions.extend(split_words_into_captions(s.words, max_chars=max_chars))
                    else:
                        text = clean(s.text)
                        if text:
                            captions.append({"start": float(s.start), "end": float(s.end), "text": text})
                    pct = 20 + round(min(1.0, s.end / total) * 78)
                    self.emit(f"{label}… {int(s.end)}s / {int(total)}s", pct)
                del model
                gc.collect()
                return captions

            try:
                self.emit(f"{label} com a GPU…", 20)
                captions = run("cuda", "float16")
            except Exception:
                self.emit(f"{label} pela CPU…", 20)
                captions = run("cpu", "int8")
        if not captions:
            raise RuntimeError("Não encontrei fala em português.")
        self.emit(f"{len(captions)} trechos gerados.", 100)
        return captions

    def transcribe(self, video: Path) -> list[dict]:
        return self._run_whisper(
            video, "transcribe", "Transcrevendo em português", CAPTION_MAX_CHARS
        )

    def _extract_audio(self, source: Path, dest: Path, channels: int, rate: int):
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(source), "-vn", "-ac", str(channels),
             "-ar", str(rate), "-c:a", "pcm_s16le", str(dest)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode:
            raise RuntimeError("Não consegui extrair o áudio desse vídeo.")

    def sync_lyrics(
        self, video: Path, lyrics: str, vocals_output: Path
    ) -> tuple[list[dict], Path | None]:
        """`vocals_output` é um caminho temporário de sessão (não persistente)
        onde a voz isolada é salva, para o usuário poder ouvi-la e revisar a
        waveform dela; cabe ao chamador apagá-lo quando não precisar mais."""
        lines = lyric_lines(lyrics)
        if not lines:
            raise RuntimeError("A letra está vazia.")
        self.ensure_tools()
        saved_vocals_path = None
        with tempfile.TemporaryDirectory(prefix="din-subtitler-") as td:
            folder = Path(td)
            mix = folder / "mix.wav"
            self.emit("Extraindo áudio…", 3)
            self._extract_audio(video, mix, 1, 16000)
            # A voz isolada dá tempos muito mais precisos; o mix completo
            # continua como apoio para versos que a separação perde.
            vocals = None
            if ensure_vocals_model(self.emit):
                try:
                    mix44 = folder / "mix44.wav"
                    self._extract_audio(video, mix44, 2, 44100)
                    vocals44 = folder / "vocals44.wav"
                    self.emit("Separando a voz do instrumental…", 12)
                    separate_vocals_file(mix44, vocals44, lambda done, count: self.emit(
                        f"Separando a voz do instrumental… {done}/{count}",
                        12 + round(done / count * 30),
                    ))
                    vocals = folder / "vocals.wav"
                    self._extract_audio(vocals44, vocals, 1, 16000)
                    try:
                        vocals_output.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(vocals44, vocals_output)
                        saved_vocals_path = vocals_output
                    except Exception:
                        saved_vocals_path = None
                except Exception:
                    vocals = None
            from faster_whisper import WhisperModel

            def run(device, compute):
                model = WhisperModel(str(MODELS_DIR / "whisper"), device=device, compute_type=compute)

                def listen(audio, vad, label, lo, hi):
                    kwargs = dict(
                        task="transcribe", beam_size=5, condition_on_previous_text=False,
                        word_timestamps=True, no_speech_threshold=0.8,
                    )
                    if vad == "default":
                        kwargs.update(vad_filter=True)
                    elif vad:
                        kwargs.update(vad_filter=True, vad_parameters=dict(
                            threshold=vad, min_silence_duration_ms=300,
                            speech_pad_ms=300,
                        ))
                    else:
                        kwargs.update(vad_filter=False)
                    segs, info = model.transcribe(str(audio), **kwargs)
                    total = max(info.duration, 0.001)
                    words = []
                    for s in segs:
                        for w in s.words or []:
                            words.append({
                                "word": w.word, "start": float(w.start), "end": float(w.end),
                            })
                        pct = lo + round(min(1.0, s.end / total) * (hi - lo))
                        self.emit(f"{label}… {int(s.end)}s / {int(total)}s", pct)
                    mark_isolated_words(words)
                    return words, total

                def coverage(*sources) -> float:
                    spans = None
                    for source in sources:
                        if not source:
                            continue
                        current = align_lyrics_to_words(lines, source)
                        spans = current if spans is None else merge_spans(spans, current)
                    if spans is None:
                        return 0.0
                    return sum(1 for s in spans if s) / len(lines)

                vocal_words, vocal_raw_words, mix_words = [], [], []
                if vocals is not None:
                    vocal_words, total = listen(vocals, "default", "Ouvindo a voz isolada", 45, 62)
                    # Passes extras só valem a pena quando ainda faltam
                    # versos para ancorar; se a voz isolada já cobriu quase
                    # tudo, pular economiza bastante tempo de processamento.
                    if coverage(vocal_words) < 0.92:
                        vocal_raw_words, total = listen(
                            vocals, None, "Reouvindo a voz isolada", 62, 74
                        )
                if coverage(vocal_words, vocal_raw_words) < 0.92:
                    if vocals is not None:
                        # Com a voz isolada como fonte principal, o mix
                        # completo sem filtro serve de apoio.
                        mix_words, total = listen(
                            mix, None, "Conferindo na música completa", 74, 85
                        )
                    else:
                        # No mix, o VAD tolerante (0.2) mantém canto que o
                        # limiar padrão descartaria como "não-fala".
                        mix_words, total = listen(mix, 0.2, "Ouvindo a música", 65, 85)
                        if not mix_words:
                            mix_words, total = listen(
                                mix, None, "Ouvindo sem filtro de voz", 65, 85
                            )
                del model
                gc.collect()
                return vocal_words, vocal_raw_words, mix_words, total

            try:
                self.emit("Ouvindo a música com a GPU…", 44)
                vocal_words, vocal_raw_words, mix_words, total = run("cuda", "float16")
            except Exception:
                self.emit("Ouvindo a música pela CPU…", 44)
                vocal_words, vocal_raw_words, mix_words, total = run("cpu", "int8")
            try:
                env = (
                    audio_envelope(vocals, emphasize_highs=False)
                    if vocals is not None else audio_envelope(mix)
                )
            except Exception:
                env = None
        if not vocal_words and not vocal_raw_words and not mix_words:
            raise RuntimeError("Não encontrei voz cantada nesse vídeo.")
        self.emit("Sincronizando os versos…", 95)
        # As três escutas erram versos diferentes; o merge preenche as
        # lacunas de uma com as âncoras das outras, respeitando a ordem.
        spans = align_lyrics_to_words(lines, vocal_words)
        for support in (vocal_raw_words, mix_words):
            if support:
                spans = merge_spans(spans, align_lyrics_to_words(lines, support))
        captions = captions_from_lyrics(lines, spans, total, env)
        captions = refine_lyric_starts(captions, env)
        captions = extend_lyric_ends(captions, total, env)
        self.emit(f"{len(captions)} versos sincronizados.", 100)
        return captions, saved_vocals_path

    def translate(self, video: Path) -> list[dict]:
        return self._run_whisper(
            video, "translate", "Traduzindo para inglês", EN_CAPTION_MAX_CHARS
        )


class Job(QObject):
    progress = Signal(str, int)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.finished.emit(self.fn(lambda t, p: self.progress.emit(t, p)))
        except Exception as e:
            self.failed.emit(str(e))


def model_is_ready() -> bool:
    model = MODELS_DIR / "whisper"
    return all((model / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json"))


def nvidia_is_available() -> bool:
    if os.name != "nt":
        return False
    try:
        ctypes.WinDLL("nvcuda.dll")
        return True
    except Exception:
        return False


def gpu_components_ready() -> bool:
    return (
        any((BIN_DIR / "nvidia" / "cublas" / "bin").glob("cublas64_*.dll"))
        and any((BIN_DIR / "nvidia" / "cudnn" / "bin").glob("cudnn*.dll"))
    )


def download_nvidia_package(package: str):
    import json
    with urllib.request.urlopen(
        f"https://pypi.org/pypi/{package}/json", timeout=30
    ) as response:
        metadata = json.load(response)
    wheel = next(
        (
            item for item in metadata["urls"]
            if item["filename"].endswith("win_amd64.whl")
        ),
        None,
    )
    if not wheel:
        raise RuntimeError(f"Não encontrei o componente Windows de {package}.")
    with tempfile.TemporaryDirectory(prefix="din-gpu-") as directory:
        wheel_path = Path(directory) / wheel["filename"]
        urllib.request.urlretrieve(wheel["url"], wheel_path)
        with zipfile.ZipFile(wheel_path) as archive:
            members = [
                name for name in archive.namelist()
                if name.startswith("nvidia/") and "/bin/" in name
            ]
            archive.extractall(BIN_DIR, members)


class ModelDownloadWorker(QObject):
    finished = Signal()
    failed = Signal(str)

    def __init__(self, include_gpu: bool):
        super().__init__()
        self.include_gpu = include_gpu

    def run(self):
        try:
            from faster_whisper.utils import download_model
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            download_model("large-v3", output_dir=str(MODELS_DIR / "whisper"))
            if self.include_gpu and not gpu_components_ready():
                download_nvidia_package("nvidia-cublas-cu12")
                download_nvidia_package("nvidia-cudnn-cu12")
            if not model_is_ready():
                raise RuntimeError("O download terminou, mas os arquivos do modelo estão incompletos.")
            self.finished.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class ComponentSetupDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preparar o Din Subtitler")
        self.setModal(True)
        self.setMinimumWidth(500)
        self.thread = None
        root = QVBoxLayout(self)
        title = QLabel("<b style='font-size:15pt;color:#ff426d'>Componentes necessários</b>")
        text = QLabel(
            "Para transcrever e traduzir, o Din Subtitler precisa baixar o modelo "
            "Whisper large-v3.<br><br>"
            "O download tem aproximadamente <b>3 GB</b> e será salvo em "
            "<b>models\\whisper</b>, dentro desta mesma pasta portable.<br><br>"
            "Em computadores com placa NVIDIA, a aceleração opcional usa aproximadamente "
            "<b>1,8 GB</b> adicionais dentro de <b>components</b>.<br><br>"
            "Isso acontece apenas uma vez. Depois, o processamento funciona localmente."
        )
        text.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.gpu_checkbox = QCheckBox(
            "Baixar também a aceleração NVIDIA (recomendado neste computador)"
        )
        self.gpu_checkbox.setChecked(nvidia_is_available())
        self.gpu_checkbox.setVisible(nvidia_is_available() and not gpu_components_ready())
        buttons = QHBoxLayout()
        self.later_btn = QPushButton("Agora não")
        self.download_btn = QPushButton("Baixar componentes")
        self.download_btn.setObjectName("primary")
        buttons.addStretch()
        buttons.addWidget(self.later_btn)
        buttons.addWidget(self.download_btn)
        root.addWidget(title)
        root.addWidget(text)
        root.addWidget(self.gpu_checkbox)
        root.addWidget(self.progress)
        root.addWidget(self.status)
        root.addLayout(buttons)
        self.later_btn.clicked.connect(self.reject)
        self.download_btn.clicked.connect(self.start_download)

    def start_download(self):
        self.download_btn.setEnabled(False)
        self.later_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText("Baixando o modelo… Isso pode levar alguns minutos.")
        self.thread = QThread(self)
        self.worker = ModelDownloadWorker(self.gpu_checkbox.isChecked())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.download_finished)
        self.worker.failed.connect(self.download_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def download_finished(self):
        activate_gpu_dlls()
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.status.setText("Componentes prontos!")
        QTimer.singleShot(350, self.accept)

    def download_failed(self, message: str):
        self.progress.setVisible(False)
        self.status.setText("Não foi possível concluir o download.")
        self.download_btn.setEnabled(True)
        self.later_btn.setEnabled(True)
        QMessageBox.critical(
            self, APP_NAME,
            "Não foi possível baixar o modelo.\n\n"
            "Verifique sua conexão com a internet e tente novamente.\n\n"
            f"Detalhes: {message}",
        )


def ensure_model(parent=None) -> bool:
    if model_is_ready():
        return True
    return ComponentSetupDialog(parent).exec() == QDialog.DialogCode.Accepted


class LyricsSearchWorker(QObject):
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, artist: str, track: str):
        super().__init__()
        self.artist, self.track = artist, track

    def run(self):
        try:
            self.finished.emit(fetch_lyrics(self.artist, self.track))
        except Exception as exc:
            self.failed.emit(str(exc))


class LyricsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sincronizar letra de música")
        self.setMinimumSize(520, 560)
        self.lyrics = ""
        self.thread = None
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(QLabel(
            "Informe o artista e a música para buscar a letra na internet,\n"
            "ou cole a letra diretamente no campo abaixo.\n"
            "Na primeira sincronização, um modelo de separação de voz\n"
            "(~64 MB) é baixado para melhorar a precisão dos tempos."
        ))
        form = QHBoxLayout()
        self.artist_edit = QLineEdit(placeholderText="Artista ou banda")
        self.track_edit = QLineEdit(placeholderText="Nome da música")
        form.addWidget(self.artist_edit, 1)
        form.addWidget(self.track_edit, 1)
        layout.addLayout(form)
        self.search_btn = QPushButton("Buscar letra na internet")
        self.search_btn.clicked.connect(self.search)
        layout.addWidget(self.search_btn)
        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        self.lyrics_edit = QPlainTextEdit(
            placeholderText="A letra aparecerá aqui.\nRevise antes de sincronizar: cada linha vira um trecho da legenda."
        )
        layout.addWidget(self.lyrics_edit, 1)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        self.sync_btn = QPushButton("Sincronizar com o vídeo", objectName="primary")
        self.sync_btn.clicked.connect(self.confirm)
        buttons.addWidget(cancel)
        buttons.addWidget(self.sync_btn)
        layout.addLayout(buttons)
        self.artist_edit.returnPressed.connect(self.search)
        self.track_edit.returnPressed.connect(self.search)

    def search(self):
        artist, track = self.artist_edit.text().strip(), self.track_edit.text().strip()
        if not artist or not track:
            self.feedback.setText("Preencha o artista e o nome da música.")
            return
        self.search_btn.setEnabled(False)
        self.feedback.setText("Buscando a letra…")
        self.thread = QThread(self)
        worker = LyricsSearchWorker(artist, track)
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run)
        worker.finished.connect(self.search_done)
        worker.failed.connect(self.search_failed)
        worker.finished.connect(self.thread.quit)
        worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.worker = worker
        self.thread.start()

    def search_done(self, lyrics: str):
        self.search_btn.setEnabled(True)
        self.feedback.setText("Letra encontrada. Revise o texto antes de sincronizar.")
        self.lyrics_edit.setPlainText(lyrics)

    def search_failed(self, message: str):
        self.search_btn.setEnabled(True)
        self.feedback.setText(message)

    def confirm(self):
        lyrics = self.lyrics_edit.toPlainText().strip()
        if not lyrics:
            self.feedback.setText("Busque ou cole a letra antes de sincronizar.")
            return
        self.lyrics = lyrics
        self.accept()


class SubtitleOverlay(QGraphicsTextItem):
    moved = Signal()
    textEdited = Signal(str)
    editingFinished = Signal()
    editingStarted = Signal()

    def __init__(self):
        super().__init__()
        self.setDefaultTextColor(Qt.GlobalColor.white)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setZValue(10)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.editing = False
        self.document().contentsChanged.connect(self._on_contents_changed)

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.moved.emit()
        return result

    def mouseDoubleClickEvent(self, event):
        if not self.editing:
            self.start_editing()
        super().mouseDoubleClickEvent(event)

    def start_editing(self):
        self.editing = True
        self._original_text = self.toPlainText()
        self.editingStarted.emit()
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def stop_editing(self):
        if not self.editing:
            return
        self.editing = False
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.editingFinished.emit()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.stop_editing()

    def keyPressEvent(self, event):
        if self.editing:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    super().keyPressEvent(event)  # Shift+Enter: quebra de linha
                    return
                self.clearFocus()
                event.accept()
                return
            if event.key() == Qt.Key.Key_Escape:
                self.setPlainText(self._original_text)
                self.clearFocus()
                event.accept()
                return
        super().keyPressEvent(event)

    def _on_contents_changed(self):
        if self.editing:
            self.textEdited.emit(self.toPlainText())


class VideoDropView(QGraphicsView):
    fileDropped = Signal(str)

    def __init__(self, scene):
        super().__init__(scene)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.fileDropped.emit(urls[0].toLocalFile())
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class ResetSlider(QSlider):
    def __init__(self, orientation, reset_value: int, parent=None):
        super().__init__(orientation, parent)
        self.reset_value = reset_value

    def mouseDoubleClickEvent(self, event):
        self.setValue(self.reset_value)
        event.accept()


class VolumeSlider(ResetSlider):
    def _show_value(self, event):
        QToolTip.showText(
            event.globalPosition().toPoint(), f"Volume: {self.value()}%", self
        )

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._show_value(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._show_value(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._show_value(event)


class SubtitleTable(QTableWidget):
    def __init__(self, empty_text: str):
        super().__init__(0, 4)
        self.empty_text = empty_text

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.rowCount() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(QColor("#7e899e"))
            font = painter.font()
            font.setPointSize(12)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.drawText(
                self.viewport().rect().adjusted(30, 30, -30, -30),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self.empty_text,
            )

    def keyPressEvent(self, event):
        if self.rowCount() and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            row = max(0, self.currentRow())
            self.setCurrentCell(row, 3)
            self.editItem(self.item(row, 3))
            event.accept()
            return
        if self.rowCount() and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            delta = -1 if event.key() == Qt.Key.Key_Up else 1
            row = max(0, min(self.rowCount() - 1, self.currentRow() + delta))
            self.setCurrentCell(row, 0)
            self.selectRow(row)
            event.accept()
            return
        super().keyPressEvent(event)


class WaveformWidget(QWidget):
    seekRequested = Signal(int)
    segmentChanged = Signal(int)
    segmentSelected = Signal(int)
    segmentEditStarted = Signal()
    selectionChanged = Signal(object)
    structureChanged = Signal(object)
    segmentEditRequested = Signal(int)
    viewChanged = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(105)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.peaks: list[float] = []
        self.peak_norm = 1.0
        self.duration = 1.0
        self.position = 0.0
        self.captions: list[dict] = []
        self.view_start = 0.0
        self.zoom = 1.0
        self.drag_mode = None
        self.drag_index = -1
        self.drag_origin = 0.0
        self.original = None
        self.selected_indices: set[int] = set()
        self.editing_index = -1
        self.rubber_start = None
        self.rubber_end = None
        self.preview_blocks: list[dict] = []
        self.press_x = 0.0
        self.pan_origin_x = 0.0
        self.pan_origin_start = 0.0
        self.pencil_cursor = self._make_pencil_cursor()
        self.start_edge_cursor = self._make_edge_cursor(left=True)
        self.end_edge_cursor = self._make_edge_cursor(left=False)
        self.setToolTip(
            "Esquerdo: reprodução  •  Direito: selecionar  •  "
            "Ctrl: criar  •  Alt: duplicar  •  Rodinha: zoom  •  "
            "Arrastar rodinha: navegar"
        )

    def set_waveform(self, peaks: list[float], duration: float, preserve_view: bool = False):
        self.peaks = peaks
        self.peak_norm = max(max(peaks, default=0.0), 0.05)
        self.duration = max(.1, duration)
        if preserve_view:
            self.view_start = max(0.0, min(self.view_start, self.duration - self.visible_duration()))
        else:
            self.view_start = 0.0
            self.zoom = 1.0
        self.viewChanged.emit()
        self.update()

    def set_view_start(self, seconds: float):
        self.view_start = max(0.0, min(seconds, self.duration - self.visible_duration()))
        self.update()

    def set_captions(self, captions: list[dict]):
        self.captions = captions
        self.selected_indices = {
            i for i in self.selected_indices if 0 <= i < len(captions)
        }
        self.update()

    def set_selected(self, indices):
        self.selected_indices = {
            int(i) for i in indices if 0 <= int(i) < len(self.captions)
        }
        self.update()

    def set_editing_index(self, index: int):
        self.editing_index = index if 0 <= index < len(self.captions) else -1
        self.update()

    @staticmethod
    def _make_pencil_cursor():
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#ffffff"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(5, 19, 18, 6)
        painter.setPen(QPen(QColor("#ff426d"), 2))
        painter.drawLine(4, 20, 8, 19)
        painter.end()
        return QCursor(pixmap, 4, 20)

    @staticmethod
    def _make_edge_cursor(left: bool):
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(
            QPen(
                QColor("#ffffff"), 2, Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.MiterJoin,
            )
        )
        x = 8 if left else 16
        direction = 7 if left else -7
        painter.drawLine(x, 4, x, 20)
        painter.drawLine(x, 4, x + direction, 4)
        painter.drawLine(x, 20, x + direction, 20)
        painter.end()
        return QCursor(pixmap, x, 12)

    def set_position(self, seconds: float):
        self.position = seconds
        self.update()

    def visible_duration(self):
        return self.duration / self.zoom

    def x_to_time(self, x):
        return self.view_start + max(0, min(self.width(), x)) / max(1, self.width()) * self.visible_duration()

    def time_to_x(self, seconds):
        return (seconds - self.view_start) / self.visible_duration() * self.width()

    def wheelEvent(self, event):
        delta = event.angleDelta()
        steps = (delta.y() or delta.x()) / 120
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            span = self.visible_duration()
            self.view_start -= steps * span * .12
        else:
            anchor = self.x_to_time(event.position().x())
            self.zoom = max(1.0, min(80.0, self.zoom * (1.25 ** steps)))
            fraction = event.position().x() / max(1, self.width())
            self.view_start = anchor - fraction * self.visible_duration()
        self.view_start = max(0.0, min(self.view_start, self.duration - self.visible_duration()))
        self.viewChanged.emit()
        self.update()
        event.accept()

    BLOCK_STRIP = 44  # altura da faixa de blocos, sobreposta à parte de baixo da onda

    def block_top_px(self):
        return self.height() - self.BLOCK_STRIP

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#0a0e15"))
        center = self.height() // 2
        if self.peaks:
            first = int(self.view_start / self.duration * len(self.peaks))
            last_time = self.view_start + self.visible_duration()
            last = min(len(self.peaks), int(last_time / self.duration * len(self.peaks)) + 1)
            p.setPen(QPen(QColor("#64748f"), 1))
            for x in range(self.width()):
                ratio = x / max(1, self.width() - 1)
                idx = first + int(ratio * max(0, last - first - 1))
                if 0 <= idx < len(self.peaks):
                    amp = min(1.0, self.peaks[idx] / self.peak_norm) * (self.height() * .46)
                    p.drawLine(x, int(center - amp), x, int(center + amp))
        p.setPen(QPen(QColor("#30394a"), 1))
        span = self.visible_duration()
        interval = 1 if span < 20 else 5 if span < 90 else 10 if span < 240 else 30
        first_tick = int(self.view_start / interval) * interval
        tick = first_tick
        while tick <= self.view_start + span:
            x = self.time_to_x(tick)
            p.drawLine(int(x), 0, int(x), self.height())
            p.setPen(QColor("#8993a8"))
            p.drawText(int(x + 3), 13, f"{int(tick)//60}:{int(tick)%60:02}")
            p.setPen(QPen(QColor("#30394a"), 1))
            tick += interval
        block_top = self.block_top_px() + 5
        block_height = self.height() - block_top - 5
        for i, c in enumerate(self.captions):
            x1, x2 = self.time_to_x(c["start"]), self.time_to_x(c["end"])
            if x2 < 0 or x1 > self.width():
                continue
            if i == self.editing_index:
                color = QColor(65, 145, 255, 220)
            elif i == self.drag_index or i in self.selected_indices:
                color = QColor(255, 66, 109, 235)
            else:
                color = QColor(147, 64, 91, 205)
            p.setBrush(QBrush(color))
            p.setPen(QPen(QColor("#ff87a1"), 1))
            p.drawRoundedRect(int(x1), block_top, max(3, int(x2 - x1)), block_height, 3, 3)
            if x2 - x1 > 18:
                p.setPen(Qt.GlobalColor.white)
                font = p.font()
                font.setPointSize(8)
                p.setFont(font)
                available = max(0, int(x2 - x1) - 8)
                elided = p.fontMetrics().elidedText(
                    c["text"].replace("\n", " "), Qt.TextElideMode.ElideRight, available
                )
                p.drawText(int(x1 + 4), block_top + block_height - 7, elided)
        if self.rubber_start is not None and self.rubber_end is not None:
            x1 = self.time_to_x(min(self.rubber_start, self.rubber_end))
            x2 = self.time_to_x(max(self.rubber_start, self.rubber_end))
            p.setBrush(QColor(67, 137, 255, 45))
            p.setPen(QPen(QColor("#65a0ff"), 1, Qt.PenStyle.DashLine))
            p.drawRect(int(x1), 1, max(1, int(x2 - x1)), self.height() - 2)
        for block in self.preview_blocks:
            x1, x2 = self.time_to_x(block["start"]), self.time_to_x(block["end"])
            p.setBrush(QColor(80, 220, 155, 100))
            p.setPen(QPen(QColor("#64e6ad"), 2, Qt.PenStyle.DashLine))
            p.drawRoundedRect(
                int(x1), block_top, max(3, int(x2 - x1)), block_height, 3, 3
            )
        play_x = self.time_to_x(self.position)
        if 0 <= play_x <= self.width():
            p.setPen(QPen(QColor("#ffd166"), 2))
            p.drawLine(int(play_x), 0, int(play_x), self.height())
        p.end()

    BOUNDARY_PX = 5
    EDGE_PX = 12
    MIN_GAP = 0.05

    def hit_segment(self, x, y):
        if y < self.block_top_px():
            return -1, None
        for i in range(len(self.captions) - 1):
            a, b = self.captions[i], self.captions[i + 1]
            if abs(a["end"] - b["start"]) < 1e-3:
                bx = self.time_to_x(a["end"])
                if abs(x - bx) <= self.BOUNDARY_PX:
                    return i, "boundary"
        edge_hits = []
        for i, c in enumerate(self.captions):
            x1, x2 = self.time_to_x(c["start"]), self.time_to_x(c["end"])
            start_distance = abs(x - x1)
            end_distance = abs(x - x2)
            if start_distance <= self.EDGE_PX:
                edge_hits.append((start_distance, 0, i, "start"))
            if end_distance <= self.EDGE_PX:
                edge_hits.append((end_distance, 1, i, "end"))
        if edge_hits:
            nearest = min(hit[0] for hit in edge_hits)
            tied = [hit for hit in edge_hits if hit[0] <= nearest + 0.5]
            _distance, _priority, index, mode = min(tied, key=lambda hit: (hit[1], hit[0]))
            return index, mode
        for i, c in enumerate(self.captions):
            x1, x2 = self.time_to_x(c["start"]), self.time_to_x(c["end"])
            if x1 < x < x2:
                return i, "move"
        return -1, None

    def neighbor_bounds(self, index):
        lower = self.captions[index - 1]["end"] if index > 0 else 0.0
        upper = self.captions[index + 1]["start"] if index < len(self.captions) - 1 else self.duration
        return lower, upper

    def mousePressEvent(self, event):
        time_at_mouse = self.x_to_time(event.position().x())
        index, mode = self.hit_segment(event.position().x(), event.position().y())
        modifiers = event.modifiers()
        self.press_x = event.position().x()
        if event.button() == Qt.MouseButton.MiddleButton:
            self.drag_mode = "pan"
            self.pan_origin_x = event.position().x()
            self.pan_origin_start = self.view_start
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.RightButton and index < 0:
            self.drag_mode = "rubber"
            self.rubber_start = time_at_mouse
            self.rubber_end = time_at_mouse
        elif (
            event.button() == Qt.MouseButton.LeftButton
            and
            modifiers & Qt.KeyboardModifier.ControlModifier
            and index < 0
        ):
            self.segmentEditStarted.emit()
            self.drag_mode = "create"
            self.drag_origin = time_at_mouse
            self.preview_blocks = [{
                "start": time_at_mouse,
                "end": time_at_mouse,
                "text": "",
            }]
        elif (
            event.button() == Qt.MouseButton.LeftButton
            and
            modifiers & Qt.KeyboardModifier.AltModifier
            and index >= 0
            and mode != "boundary"
        ):
            if index not in self.selected_indices:
                self.selected_indices = {index}
                self.selectionChanged.emit(sorted(self.selected_indices))
            indices = sorted(self.selected_indices)
            self.segmentEditStarted.emit()
            self.drag_mode = "copy"
            self.drag_origin = time_at_mouse
            self.original = [deepcopy(self.captions[i]) for i in indices]
            self.preview_blocks = deepcopy(self.original)
            self.drag_index = index
        elif event.button() == Qt.MouseButton.LeftButton and index >= 0:
            self.segmentEditStarted.emit()
            self.drag_index, self.drag_mode = index, mode
            self.drag_origin = time_at_mouse
            c = self.captions[index]
            self.original = (c["start"], c["end"])
            if index not in self.selected_indices:
                self.selected_indices = {index}
                self.selectionChanged.emit([index])
            self.segmentSelected.emit(index)
            seek_time = c["end"] if mode == "boundary" else c["start"]
            self.seekRequested.emit(round(seek_time * 1000))
        elif event.button() == Qt.MouseButton.LeftButton:
            self.drag_mode = "playhead"
            self.position = time_at_mouse
            self.selected_indices.clear()
            self.selectionChanged.emit([])
            self.seekRequested.emit(round(self.position * 1000))
        self.update()

    def mouseMoveEvent(self, event):
        if self.drag_mode is None:
            index, mode = self.hit_segment(event.position().x(), event.position().y())
            if (
                event.modifiers() & Qt.KeyboardModifier.ControlModifier
                and index < 0
            ):
                self.setCursor(self.pencil_cursor)
            elif (
                event.modifiers() & Qt.KeyboardModifier.AltModifier
                and index >= 0
            ):
                self.setCursor(Qt.CursorShape.DragCopyCursor)
            elif mode == "start":
                self.setCursor(self.start_edge_cursor)
            elif mode == "end":
                self.setCursor(self.end_edge_cursor)
            elif mode == "boundary":
                self.setCursor(Qt.CursorShape.SplitHCursor)
            elif mode == "move":
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        now = self.x_to_time(event.position().x())
        if self.drag_mode == "pan":
            seconds_per_pixel = self.visible_duration() / max(1, self.width())
            self.view_start = self.pan_origin_start - (
                event.position().x() - self.pan_origin_x
            ) * seconds_per_pixel
            self.view_start = max(
                0.0, min(self.view_start, self.duration - self.visible_duration())
            )
            self.viewChanged.emit()
        elif self.drag_mode == "playhead":
            self.position = now
            self.seekRequested.emit(round(now * 1000))
        elif self.drag_mode == "rubber":
            self.rubber_end = now
        elif self.drag_mode == "create":
            self.preview_blocks[0]["start"] = min(self.drag_origin, now)
            self.preview_blocks[0]["end"] = max(self.drag_origin, now)
        elif self.drag_mode == "copy":
            earliest = min(c["start"] for c in self.original)
            latest = max(c["end"] for c in self.original)
            delta = max(-earliest, min(now - self.drag_origin, self.duration - latest))
            self.preview_blocks = [
                {
                    "start": c["start"] + delta,
                    "end": c["end"] + delta,
                    "text": c["text"],
                }
                for c in self.original
            ]
        elif self.drag_mode == "boundary":
            i = self.drag_index
            a, b = self.captions[i], self.captions[i + 1]
            lower, upper = a["start"] + self.MIN_GAP, b["end"] - self.MIN_GAP
            t = max(lower, min(now, upper))
            a["end"] = t
            b["start"] = t
            self.segmentChanged.emit(i)
            self.segmentChanged.emit(i + 1)
        elif self.drag_index >= 0:
            c = self.captions[self.drag_index]
            lower_bound, upper_bound = self.neighbor_bounds(self.drag_index)
            if self.drag_mode == "start":
                c["start"] = max(lower_bound, min(now, c["end"] - self.MIN_GAP))
            elif self.drag_mode == "end":
                c["end"] = min(upper_bound, max(now, c["start"] + self.MIN_GAP))
            else:
                start, end = self.original
                delta, length = now - self.drag_origin, end - start
                new_start = max(lower_bound, min(start + delta, upper_bound - length))
                c["start"], c["end"] = new_start, new_start + length
            self.segmentChanged.emit(self.drag_index)
        self.update()

    def mouseReleaseEvent(self, event):
        if self.drag_mode == "rubber":
            start, end = sorted((self.rubber_start, self.rubber_end))
            self.selected_indices = {
                i for i, c in enumerate(self.captions)
                if c["end"] > start and c["start"] < end
            }
            self.selectionChanged.emit(sorted(self.selected_indices))
        elif self.drag_mode == "create":
            block = self.preview_blocks[0]
            if block["end"] - block["start"] >= self.MIN_GAP:
                self._insert_with_crop([block])
        elif self.drag_mode == "copy":
            if self.preview_blocks and any(
                abs(a["start"] - b["start"]) >= .001
                for a, b in zip(self.preview_blocks, self.original)
            ):
                self._insert_with_crop(self.preview_blocks)
        self.drag_mode = None
        self.drag_index = -1
        self.original = None
        self.rubber_start = None
        self.rubber_end = None
        self.preview_blocks = []
        self.unsetCursor()
        self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            index, _mode = self.hit_segment(event.position().x(), event.position().y())
            if index >= 0:
                self.segmentEditRequested.emit(index)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def _insert_with_crop(self, new_blocks: list[dict]):
        existing = list(self.captions)
        for new in sorted(new_blocks, key=lambda c: c["start"]):
            cropped = []
            ns, ne = new["start"], new["end"]
            for old in existing:
                os_, oe = old["start"], old["end"]
                if oe <= ns or os_ >= ne:
                    cropped.append(old)
                elif os_ >= ns and oe <= ne:
                    continue
                elif os_ < ns and oe > ne:
                    left, right = ns - os_, oe - ne
                    if left >= right:
                        old["end"] = ns
                    else:
                        old["start"] = ne
                    if old["end"] - old["start"] >= self.MIN_GAP:
                        cropped.append(old)
                elif os_ < ns < oe:
                    old["end"] = ns
                    if old["end"] - old["start"] >= self.MIN_GAP:
                        cropped.append(old)
                elif os_ < ne < oe:
                    old["start"] = ne
                    if old["end"] - old["start"] >= self.MIN_GAP:
                        cropped.append(old)
            existing = cropped
        inserted = [deepcopy(block) for block in new_blocks]
        combined = existing + inserted
        combined.sort(key=lambda c: (c["start"], c["end"]))
        self.captions[:] = combined
        self.selected_indices = {
            i for i, caption in enumerate(self.captions)
            if any(caption is block for block in inserted)
        }
        # deepcopy above means identity survives the sort inside combined.
        if not self.selected_indices:
            self.selected_indices = {
                i for i, caption in enumerate(self.captions)
                if caption in inserted
            }
        selected = sorted(self.selected_indices)
        self.structureChanged.emit(selected)
        self.selectionChanged.emit(selected)


class WaveformPanel(QWidget):
    seekRequested = Signal(int)
    segmentChanged = Signal(int)
    segmentSelected = Signal(int)
    segmentEditStarted = Signal()
    selectionChanged = Signal(object)
    structureChanged = Signal(object)
    segmentEditRequested = Signal(int)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.waveform = WaveformWidget()
        self.scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        self.scrollbar.setEnabled(False)
        layout.addWidget(self.waveform, 1)
        layout.addWidget(self.scrollbar)
        self._updating = False
        self.waveform.seekRequested.connect(self.seekRequested)
        self.waveform.segmentChanged.connect(self.segmentChanged)
        self.waveform.segmentSelected.connect(self.segmentSelected)
        self.waveform.segmentEditStarted.connect(self.segmentEditStarted)
        self.waveform.selectionChanged.connect(self.selectionChanged)
        self.waveform.structureChanged.connect(self.structureChanged)
        self.waveform.segmentEditRequested.connect(self.segmentEditRequested)
        self.waveform.viewChanged.connect(self.sync_scrollbar)
        self.scrollbar.valueChanged.connect(self.on_scrollbar)

    def set_waveform(self, peaks: list[float], duration: float, preserve_view: bool = False):
        self.waveform.set_waveform(peaks, duration, preserve_view)

    def set_captions(self, captions: list[dict]):
        self.waveform.set_captions(captions)

    def set_position(self, seconds: float):
        self.waveform.set_position(seconds)

    def sync_scrollbar(self):
        span = self.waveform.visible_duration()
        max_start = max(0.0, self.waveform.duration - span)
        self._updating = True
        self.scrollbar.setEnabled(max_start > 0.01)
        self.scrollbar.setRange(0, round(max_start * 1000))
        self.scrollbar.setPageStep(round(span * 1000))
        self.scrollbar.setSingleStep(max(1, round(span * 100)))
        self.scrollbar.setValue(round(self.waveform.view_start * 1000))
        self._updating = False

    def on_scrollbar(self, value):
        if self._updating:
            return
        self.waveform.set_view_start(value / 1000)


class CaptionTextEdit(QPlainTextEdit):
    commitRequested = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)  # Shift+Enter: quebra de linha
            else:
                self.commitRequested.emit()
            return
        super().keyPressEvent(event)


class CaptionDelegate(QStyledItemDelegate):
    liveText = Signal(int, str)
    editingStarted = Signal(int)

    def createEditor(self, parent, option, index):
        self.editingStarted.emit(index.row())
        editor = CaptionTextEdit(parent)
        editor.setFrameShape(QFrame.Shape.NoFrame)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor.commitRequested.connect(
            lambda: (
                self.commitData.emit(editor),
                self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint),
            )
        )
        editor.textChanged.connect(
            lambda row=index.row(): self.liveText.emit(row, editor.toPlainText())
        )
        return editor

    def setEditorData(self, editor, index):
        editor.setPlainText(index.data() or "")
        cursor = editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        editor.setTextCursor(cursor)

    def setModelData(self, editor, model, index):
        model.setData(index, clean_lines(editor.toPlainText()))

    def updateEditorGeometry(self, editor, option, index):
        rect = option.rect
        rect.setHeight(max(rect.height(), 64))
        editor.setGeometry(rect)


class SubtitleEditor(QWidget):
    seek = Signal(int)
    changed = Signal()
    selectionChanged = Signal(object)
    editingStarted = Signal(int)
    editingFinished = Signal()

    def __init__(self, language: str, empty_text: str):
        super().__init__()
        self.language = language
        self.captions: list[dict] = []
        self.loading = False
        self.following = False
        self.undo_stack: list[list[dict]] = []
        self.redo_stack: list[list[dict]] = []
        root = QVBoxLayout(self)
        self.table = SubtitleTable(empty_text)
        self.table.setHorizontalHeaderLabels(["#", "Início", "Fim", "Texto"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 45)
        self.table.setColumnWidth(1, 115)
        self.table.setColumnWidth(2, 115)
        self.table.setMinimumHeight(140)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
        )
        self.caption_delegate = CaptionDelegate(self)
        self.table.setItemDelegateForColumn(3, self.caption_delegate)
        self.caption_delegate.liveText.connect(self.on_live_text)
        self.caption_delegate.editingStarted.connect(self.begin_editing)
        self.caption_delegate.closeEditor.connect(
            lambda *_args: self.editingFinished.emit()
        )
        root.addWidget(self.table, 1)
        self.table.itemSelectionChanged.connect(self.on_selection)
        self.table.itemChanged.connect(self.on_item_changed)

    def set_captions(self, captions: list[dict]):
        self.captions = [dict(x) for x in captions]
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.refresh()

    def push_undo(self):
        snapshot = deepcopy(self.captions)
        if not self.undo_stack or self.undo_stack[-1] != snapshot:
            self.undo_stack.append(snapshot)
            self.redo_stack.clear()
            if len(self.undo_stack) > 100:
                del self.undo_stack[0]

    def undo(self):
        if not self.undo_stack:
            return False
        row = max(0, self.table.currentRow())
        self.redo_stack.append(deepcopy(self.captions))
        self.captions = self.undo_stack.pop()
        self.refresh(row)
        self.changed.emit()
        return True

    def redo(self):
        if not self.redo_stack:
            return False
        row = max(0, self.table.currentRow())
        self.undo_stack.append(deepcopy(self.captions))
        self.captions = self.redo_stack.pop()
        self.refresh(row)
        self.changed.emit()
        return True

    def begin_editing(self, row: int):
        self.push_undo()
        self.editingStarted.emit(row)

    def edit_segment(self, row: int):
        if not (0 <= row < len(self.captions)):
            return
        self.select_segment(row)
        self.table.setCurrentCell(row, 3)
        self.table.editItem(self.table.item(row, 3))

    def commit_text(self):
        row = self.table.currentRow()
        editor = QApplication.focusWidget()
        if (
            row >= 0 and row < len(self.captions)
            and isinstance(editor, QPlainTextEdit)
            and self.table.isAncestorOf(editor)
        ):
            text = clean_lines(editor.toPlainText())
            self.captions[row]["text"] = text
            self.loading = True
            self.table.item(row, 3).setText(text)
            self.loading = False
            self.table.resizeRowToContents(row)

    def refresh(self, select=0):
        self.loading = True
        self.table.setRowCount(len(self.captions))
        for r, c in enumerate(self.captions):
            values = [str(r + 1), editor_time(c["start"]), editor_time(c["end"]), c["text"]]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col < 3:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(r, col, item)
        self.loading = False
        self.table.resizeRowsToContents()
        if self.captions:
            self.table.selectRow(max(0, min(select, len(self.captions) - 1)))

    def on_selection(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        self.selectionChanged.emit(rows)
        if len(rows) == 1:
            r = rows[0]
            if not self.following:
                self.seek.emit(round(self.captions[r]["start"] * 1000))

    def follow_position(self, seconds: float):
        if not self.captions:
            return
        current = next(
            (i for i, c in enumerate(self.captions)
             if c["start"] <= seconds < c["end"]),
            -1,
        )
        if current >= 0 and current != self.table.currentRow():
            self.following = True
            self.table.selectRow(current)
            self.table.scrollToItem(self.table.item(current, 0))
            self.following = False

    def on_item_changed(self, item):
        if not self.loading and item.column() == 3:
            self.captions[item.row()]["text"] = clean_lines(item.text())
            self.table.resizeRowToContents(item.row())
            self.changed.emit()

    def on_live_text(self, row: int, text: str):
        if 0 <= row < len(self.captions):
            self.captions[row]["text"] = text
            self.changed.emit()

    def set_row_text(self, row: int, text: str):
        if not (0 <= row < len(self.captions)):
            return
        self.captions[row]["text"] = text
        self.loading = True
        self.table.item(row, 3).setText(text)
        self.loading = False
        self.changed.emit()

    def commit_row_text(self, row: int):
        if not (0 <= row < len(self.captions)):
            return
        text = clean_lines(self.captions[row]["text"])
        self.captions[row]["text"] = text
        self.loading = True
        self.table.item(row, 3).setText(text)
        self.loading = False
        self.table.resizeRowToContents(row)

    def merge(self):
        self.commit_text()
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if len(rows) != 2 or rows[1] != rows[0] + 1:
            QMessageBox.information(self, APP_NAME, "Selecione exatamente dois trechos consecutivos.")
            return
        self.push_undo()
        a, b = rows
        self.captions[a] = {
            "start": self.captions[a]["start"], "end": self.captions[b]["end"],
            "text": clean(self.captions[a]["text"] + " " + self.captions[b]["text"]),
        }
        del self.captions[b]
        self.refresh(a)
        self.changed.emit()

    def delete_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self.push_undo()
        for r in rows:
            if 0 <= r < len(self.captions):
                del self.captions[r]
        target = min(rows[-1], len(self.captions) - 1)
        self.refresh(max(0, target))
        self.changed.emit()

    def split_row(self, row: int, left: str, right: str) -> bool:
        left, right = clean_lines(left), clean_lines(right)
        if not (0 <= row < len(self.captions)) or not left or not right:
            return False
        self.push_undo()
        c = self.captions[row]
        middle = (c["start"] + c["end"]) / 2
        self.captions[row:row + 1] = [
            {"start": c["start"], "end": middle, "text": left},
            {"start": middle, "end": c["end"], "text": right},
        ]
        self.refresh(row)
        self.changed.emit()
        return True

    def split(self):
        row = self.table.currentRow()
        if row < 0:
            return
        editor = QApplication.focusWidget()
        if not isinstance(editor, QPlainTextEdit) or not self.table.isAncestorOf(editor):
            QMessageBox.information(
                self, APP_NAME,
                "Clique no texto do trecho, posicione o cursor e pressione F5."
            )
            return
        raw = editor.toPlainText()
        pos = editor.textCursor().position()
        if not self.split_row(row, raw[:pos], raw[pos:]):
            QMessageBox.information(self, APP_NAME, "Posicione o cursor entre duas partes do texto.")

    def update_timing(self, row: int):
        if not (0 <= row < len(self.captions)):
            return
        self.loading = True
        self.table.item(row, 1).setText(editor_time(self.captions[row]["start"]))
        self.table.item(row, 2).setText(editor_time(self.captions[row]["end"]))
        self.loading = False

    def select_segment(self, row: int):
        if 0 <= row < len(self.captions):
            self.following = True
            self.table.selectRow(row)
            self.table.scrollToItem(self.table.item(row, 0))
            self.following = False

    def select_segments(self, rows):
        valid = sorted({int(row) for row in rows if 0 <= int(row) < len(self.captions)})
        self.following = True
        self.table.clearSelection()
        if valid:
            self.table.setCurrentCell(valid[0], 0)
        selection = self.table.selectionModel()
        for row in valid:
            selection.select(
                self.table.model().index(row, 0),
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows,
            )
        if valid:
            self.table.scrollToItem(self.table.item(valid[0], 0))
        self.following = False


class MainWindow(QMainWindow):
    waveformReady = Signal(object, float)
    vocalsWaveformReady = Signal(object, float)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1240, 820)
        self.setAcceptDrops(True)
        self.video: Path | None = None
        self.pt: list[dict] = []
        self.en: list[dict] = []
        self.lyric: list[dict] = []
        self.thread = None
        self.busy = False
        self.overlay_updating = False
        self.loading_first_frame = False
        self.user_muted = False
        self.vocals_path: Path | None = None
        self.vocals_counter = 0
        # Voz isolada é temporária: fica num diretório da sessão, apagado ao
        # trocar de vídeo e ao fechar o programa, nunca persistida em disco.
        self.session_temp_dir = Path(
            tempfile.mkdtemp(prefix="din-subtitler-session-")
        )
        self.original_waveform: tuple[list[float], float] | None = None
        self.vocals_waveform: tuple[list[float], float] | None = None
        self.isolated_active = False
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(.8)
        self.vocals_player = QMediaPlayer(self)
        self.vocals_audio = QAudioOutput(self)
        self.vocals_player.setAudioOutput(self.vocals_audio)
        self.vocals_audio.setVolume(.8)
        self.vocals_audio.setMuted(True)
        self.build()
        self.style()
        self.setAcceptDrops(True)
        QApplication.instance().installEventFilter(self)
        for widget in self.findChildren(QWidget):
            widget.setAcceptDrops(True)
            widget.installEventFilter(self)

    def style(self):
        self.setStyleSheet("""
        QMainWindow,QWidget { background:#0d1017; color:#e8ebf2; font:10pt "Segoe UI"; }
        QLabel { background:transparent; }
        QLabel#brand { font:13pt "Segoe UI Semibold"; letter-spacing:2px; }
        QLabel#sectionTitle { color:#8b96ab; font:9pt "Segoe UI Semibold"; letter-spacing:1px; }
        QFrame#card { background:#151a24; border:1px solid #2c3444; border-radius:10px; }
        QFrame#section { background:#111620; border:1px solid #30394b; border-radius:8px; }
        QPushButton { background:#252c3a; border:0; border-radius:6px; padding:7px 12px; }
        QPushButton:hover { background:#333c4e; }
        QPushButton:pressed { background:#3d4759; }
        QPushButton#primary { background:#ff426d; color:white; font-weight:600; }
        QPushButton#primary:hover { background:#ff5c81; }
        QPushButton#primary:pressed { background:#e03a61; }
        QPushButton:disabled { color:#657084; background:#1b202b; }
        QComboBox, QFontComboBox, QSpinBox {
          background:#1a202c; border:1px solid #313a4c; border-radius:6px; padding:4px 6px; }
        QComboBox:hover, QSpinBox:hover { border-color:#4a5468; }
        QComboBox::drop-down { border:0; width:22px; }
        QComboBox::down-arrow { image:none; border-left:4px solid transparent;
          border-right:4px solid transparent; border-top:5px solid #8993a8; margin-right:7px; }
        QComboBox QAbstractItemView { background:#1a202c; border:1px solid #313a4c;
          selection-background-color:#7e2941; outline:0; }
        QTableWidget { background:#121720; alternate-background-color:#171d28;
          gridline-color:#252d3b; selection-background-color:#7e2941; border:0; }
        QHeaderView::section { background:#1a2130; padding:8px; border:0; font-weight:600; }
        QTableCornerButton::section { background:#1a2130; border:0; }
        QPlainTextEdit { background:#121720; border:1px solid #313a4c; border-radius:6px; padding:6px; }
        QSlider::groove:horizontal { height:5px; background:#293141; border-radius:2px; }
        QSlider::sub-page:horizontal { height:5px; background:#8f2c49; border-radius:2px; }
        QSlider::handle:horizontal { width:14px; margin:-5px 0; background:#ff426d; border-radius:7px; }
        QSlider::handle:horizontal:hover { background:#ff5c81; }
        QSplitter::handle { background:#1c2330; }
        QSplitter::handle:horizontal { width:5px; }
        QSplitter::handle:vertical { height:5px; }
        QTabWidget::pane { border:0; }
        QTabBar::tab { background:#1b212d; padding:8px 20px; margin-right:2px;
          border-top-left-radius:6px; border-top-right-radius:6px; }
        QTabBar::tab:selected { background:#ff426d; color:white; font-weight:600; }
        QTabBar::tab:!selected:hover { background:#252d3c; }
        QScrollBar:vertical { background:transparent; width:11px; margin:0; }
        QScrollBar::handle:vertical { background:#323b4d; border-radius:5px; min-height:30px; }
        QScrollBar::handle:vertical:hover { background:#414c62; }
        QScrollBar:horizontal { background:transparent; height:11px; margin:0; }
        QScrollBar::handle:horizontal { background:#323b4d; border-radius:5px; min-width:30px; }
        QScrollBar::handle:horizontal:hover { background:#414c62; }
        QScrollBar::add-line, QScrollBar::sub-line { height:0; width:0; }
        QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }
        QProgressBar { background:#1a202c; border:0; border-radius:8px;
          min-height:17px; max-height:17px; font-size:8pt; color:white; }
        QProgressBar::chunk { background:#ff426d; border-radius:8px; }
        QToolTip { background:#1a202c; color:#e8ebf2; border:1px solid #313a4c; padding:4px; }
        """)

    def build(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        title = QHBoxLayout()
        title.addWidget(QLabel("<b style='color:#ff426d'>DIN</b> SUBTITLER", objectName="brand"))
        title.addStretch()
        layout.addLayout(title)
        top = QSplitter(Qt.Orientation.Horizontal)
        top.setChildrenCollapsible(False)
        player_card = QFrame(objectName="card")
        player_layout = QVBoxLayout(player_card)
        player_layout.setContentsMargins(6, 6, 6, 6)
        player_layout.setSpacing(5)
        self.video_scene = QGraphicsScene(self)
        self.video_scene.setSceneRect(0, 0, 1280, 720)
        self.video_view = VideoDropView(self.video_scene)
        self.video_view.setStyleSheet("background:#030405;border:0")
        self.video_view.setFrameShape(QFrame.Shape.NoFrame)
        self.video_view.setMinimumHeight(250)
        self.video_view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.video_item = QGraphicsVideoItem()
        self.video_scene.addItem(self.video_item)
        self.subtitle_overlay = SubtitleOverlay()
        self.video_scene.addItem(self.subtitle_overlay)
        self.video_placeholder = QGraphicsTextItem("Arraste o vídeo para abrir")
        self.video_placeholder.setFont(QFont("Segoe UI", 28, QFont.Weight.DemiBold))
        self.video_placeholder.setDefaultTextColor(QColor("#778196"))
        self.video_placeholder.setZValue(5)
        placeholder_bounds = self.video_placeholder.boundingRect()
        self.video_placeholder.setPos(
            (1280 - placeholder_bounds.width()) / 2,
            (720 - placeholder_bounds.height()) / 2,
        )
        self.video_scene.addItem(self.video_placeholder)
        self.subtitle_overlay.moved.connect(self.overlay_moved)
        self.subtitle_overlay.textEdited.connect(self.overlay_text_edited)
        self.subtitle_overlay.editingFinished.connect(self.overlay_editing_finished)
        self.video_item.nativeSizeChanged.connect(self.video_size_changed)
        self.player.setVideoOutput(self.video_item)
        player_layout.addWidget(self.video_view, 1)
        transport = QHBoxLayout()
        transport.setContentsMargins(0, 0, 0, 0)
        self.play_btn = QPushButton("▶")
        self.play_btn.setFixedSize(42, 30)
        self.time_label = QLabel("00:00 / 00:00")
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        transport.addWidget(self.play_btn)
        transport.addWidget(self.seek_slider, 1)
        transport.addWidget(self.time_label)
        transport.addSpacing(10)
        self.volume_btn = QPushButton("🔊")
        self.volume_btn.setFixedSize(32, 30)
        self.volume_btn.setToolTip("Mutar / desmutar")
        self.volume_btn.clicked.connect(self.toggle_mute)
        transport.addWidget(self.volume_btn)
        self.volume_slider = VolumeSlider(Qt.Orientation.Horizontal, 80)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.setToolTip("Volume")
        self.volume_slider.valueChanged.connect(self.set_players_volume)
        transport.addWidget(self.volume_slider)
        transport.addSpacing(10)
        self.isolated_toggle = QCheckBox("Voz isolada")
        self.isolated_toggle.setEnabled(False)
        self.isolated_toggle.setToolTip(
            "Toca o áudio e mostra a waveform da voz isolada\n"
            "gerada na última sincronização de letra de música."
        )
        self.isolated_toggle.toggled.connect(self.toggle_isolated_audio)
        transport.addWidget(self.isolated_toggle)
        player_layout.addLayout(transport)
        preview = QHBoxLayout()
        preview.setContentsMargins(2, 2, 2, 2)
        preview.setSpacing(8)
        preview.addWidget(QLabel("Fonte"))
        self.font_box = QFontComboBox()
        self.font_box.setEditable(False)
        self.font_box.setCurrentFont(QFont("Arial"))
        self.font_box.setFixedWidth(170)
        preview.addWidget(self.font_box)
        self.font_size = QSpinBox()
        self.font_size.setRange(18, 100)
        self.font_size.setValue(35)
        self.font_size.setFixedWidth(48)
        self.font_size.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.font_size.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.font_size.setToolTip("Tamanho da fonte")
        preview.addWidget(self.font_size)
        self.font_size_slider = ResetSlider(Qt.Orientation.Horizontal, 35)
        self.font_size_slider.setRange(18, 100)
        self.font_size_slider.setValue(35)
        self.font_size_slider.setFixedWidth(120)
        preview.addWidget(self.font_size_slider)
        self.font_size.valueChanged.connect(self.font_size_slider.setValue)
        self.font_size_slider.valueChanged.connect(self.font_size.setValue)
        preview.addSpacing(14)
        preview.addWidget(QLabel("X"))
        self.subtitle_x_spin = QSpinBox()
        self.subtitle_x_spin.setRange(0, 100)
        self.subtitle_x_spin.setValue(50)
        self.subtitle_x_spin.setSuffix(" %")
        self.subtitle_x_spin.setFixedWidth(60)
        self.subtitle_x_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.subtitle_x_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.addWidget(self.subtitle_x_spin)
        self.subtitle_x = ResetSlider(Qt.Orientation.Horizontal, 50)
        self.subtitle_x.setRange(0, 100)
        self.subtitle_x.setValue(50)
        self.subtitle_x.setFixedWidth(150)
        preview.addWidget(self.subtitle_x)
        preview.addSpacing(14)
        preview.addWidget(QLabel("Y"))
        self.subtitle_y_spin = QSpinBox()
        self.subtitle_y_spin.setRange(0, 100)
        self.subtitle_y_spin.setValue(65)
        self.subtitle_y_spin.setSuffix(" %")
        self.subtitle_y_spin.setFixedWidth(60)
        self.subtitle_y_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.subtitle_y_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.addWidget(self.subtitle_y_spin)
        self.subtitle_y = ResetSlider(Qt.Orientation.Horizontal, 65)
        self.subtitle_y.setRange(0, 100)
        self.subtitle_y.setValue(65)
        self.subtitle_y.setFixedWidth(150)
        preview.addWidget(self.subtitle_y)
        preview.addStretch()
        player_layout.addLayout(preview)
        self.subtitle_x.valueChanged.connect(self.subtitle_x_spin.setValue)
        self.subtitle_x_spin.valueChanged.connect(self.subtitle_x.setValue)
        self.subtitle_y.valueChanged.connect(self.subtitle_y_spin.setValue)
        self.subtitle_y_spin.valueChanged.connect(self.subtitle_y.setValue)
        waveform_card = QFrame(objectName="card")
        waveform_layout = QVBoxLayout(waveform_card)
        waveform_layout.setContentsMargins(6, 6, 6, 6)
        self.waveform = WaveformPanel()
        waveform_layout.addWidget(self.waveform)
        self.media_splitter = QSplitter(Qt.Orientation.Vertical)
        self.media_splitter.setChildrenCollapsible(False)
        self.media_splitter.addWidget(player_card)
        self.media_splitter.addWidget(waveform_card)
        self.media_splitter.setStretchFactor(0, 4)
        self.media_splitter.setStretchFactor(1, 1)
        self.media_splitter.setSizes([390, 135])
        top.addWidget(self.media_splitter)
        actions = QFrame(objectName="card")
        actions.setMinimumWidth(300)
        av = QVBoxLayout(actions)
        av.setContentsMargins(8, 8, 8, 8)
        av.setSpacing(6)
        self.file_label = QLabel("Arraste um vídeo em qualquer lugar\nou clique em Carregar vídeo")
        self.file_label.setWordWrap(True)
        av.addWidget(self.file_label)
        load = QPushButton("Carregar vídeo")
        load.clicked.connect(self.choose_video)
        av.addWidget(load)
        av.addSpacing(8)
        transcription = QFrame(objectName="section")
        tv = QVBoxLayout(transcription)
        tv.setContentsMargins(8, 8, 8, 8)
        tv.setSpacing(5)
        tv.addWidget(QLabel("TRANSCRIÇÃO EM PORTUGUÊS", objectName="sectionTitle"))
        self.transcribe_btn = QPushButton("1. Transcrever para português", objectName="primary")
        self.open_pt_btn = QPushButton("Abrir SRT em português")
        self.save_pt_btn = QPushButton("Salvar SRT em português")
        tv.addWidget(self.transcribe_btn)
        tv.addWidget(self.open_pt_btn)
        tv.addWidget(self.save_pt_btn)
        av.addWidget(transcription)
        translation = QFrame(objectName="section")
        ev = QVBoxLayout(translation)
        ev.setContentsMargins(8, 8, 8, 8)
        ev.setSpacing(5)
        ev.addWidget(QLabel("TRADUÇÃO PARA INGLÊS", objectName="sectionTitle"))
        self.translate_btn = QPushButton("2. Traduzir para inglês", objectName="primary")
        self.open_en_btn = QPushButton("Abrir SRT em inglês")
        self.save_en_btn = QPushButton("Salvar SRT em inglês")
        ev.addWidget(self.translate_btn)
        ev.addWidget(self.open_en_btn)
        ev.addWidget(self.save_en_btn)
        av.addWidget(translation)
        music = QFrame(objectName="section")
        mv = QVBoxLayout(music)
        mv.setContentsMargins(8, 8, 8, 8)
        mv.setSpacing(5)
        mv.addWidget(QLabel("LETRA DE MÚSICA", objectName="sectionTitle"))
        self.lyrics_btn = QPushButton("3. Sincronizar letra de música", objectName="primary")
        self.open_lyric_btn = QPushButton("Abrir SRT da letra")
        self.save_lyric_btn = QPushButton("Salvar SRT da letra")
        mv.addWidget(self.lyrics_btn)
        mv.addWidget(self.open_lyric_btn)
        mv.addWidget(self.save_lyric_btn)
        av.addWidget(music)
        av.addStretch()
        top.addWidget(actions)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 0)
        top.setSizes([920, 320])
        self.editors = QTabWidget()
        self.pt_editor = SubtitleEditor(
            "português",
            "Transcreva o vídeo para gerar a legenda em português\nou abra um arquivo SRT existente.",
        )
        self.en_editor = SubtitleEditor(
            "inglês",
            "Traduza o vídeo para gerar a legenda em inglês\nou abra um arquivo SRT existente.",
        )
        self.lyric_editor = SubtitleEditor(
            "letra",
            "Sincronize a letra de uma música cantada no vídeo\nou abra um arquivo SRT existente.",
        )
        self.editors.addTab(self.pt_editor, "Português")
        self.editors.addTab(self.en_editor, "English")
        self.editors.addTab(self.lyric_editor, "Letra")
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 6, 0)
        corner_layout.setSpacing(6)
        self.merge_btn = QPushButton("Juntar  ·  F4")
        self.split_btn = QPushButton("Separar no cursor  ·  F5")
        corner_layout.addWidget(self.merge_btn)
        corner_layout.addWidget(self.split_btn)
        self.editors.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        self.workspace = QSplitter(Qt.Orientation.Vertical)
        self.workspace.setChildrenCollapsible(False)
        self.workspace.addWidget(top)
        self.workspace.addWidget(self.editors)
        self.workspace.setStretchFactor(0, 3)
        self.workspace.setStretchFactor(1, 2)
        self.workspace.setSizes([470, 360])
        layout.addWidget(self.workspace, 1)
        status = QHBoxLayout()
        self.status = QLabel("Pronto")
        self.progress = QProgressBar()
        self.progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress.setMaximumWidth(300)
        self.progress.setVisible(False)
        status.addWidget(self.status)
        status.addStretch()
        status.addWidget(self.progress)
        layout.addLayout(status)
        self.setCentralWidget(central)
        self.play_btn.clicked.connect(self.toggle_play)
        self.seek_slider.sliderMoved.connect(self.player.setPosition)
        self.player.positionChanged.connect(self.position_changed)
        self.player.durationChanged.connect(lambda d: self.seek_slider.setRange(0, d))
        self.player.mediaStatusChanged.connect(self.media_status_changed)
        self.player.playbackStateChanged.connect(
            lambda s: self.play_btn.setText("❚❚" if s == QMediaPlayer.PlaybackState.PlayingState else "▶")
        )
        self.player.playbackStateChanged.connect(self.mirror_playback_state)
        self.transcribe_btn.clicked.connect(self.transcribe)
        self.translate_btn.clicked.connect(self.translate)
        self.lyrics_btn.clicked.connect(self.sync_lyrics)
        self.open_pt_btn.clicked.connect(lambda: self.open_subtitle("pt"))
        self.open_en_btn.clicked.connect(lambda: self.open_subtitle("en"))
        self.open_lyric_btn.clicked.connect(lambda: self.open_subtitle("lyric"))
        self.save_pt_btn.clicked.connect(lambda: self.save("pt"))
        self.save_en_btn.clicked.connect(lambda: self.save("en"))
        self.save_lyric_btn.clicked.connect(lambda: self.save("lyric"))
        self.merge_btn.clicked.connect(lambda: self.active_editor().merge())
        self.split_btn.clicked.connect(self.trigger_split)
        QShortcut(QKeySequence("F4"), self, activated=lambda: self.active_editor().merge())
        QShortcut(QKeySequence("F5"), self, activated=self.trigger_split)
        for editor in (self.pt_editor, self.en_editor, self.lyric_editor):
            editor.seek.connect(self.player.setPosition)
            editor.changed.connect(self.update_subtitle_preview)
            editor.changed.connect(self.editor_changed)
            editor.selectionChanged.connect(
                lambda rows, source=editor: self.editor_selection_changed(source, rows)
            )
        self.editors.currentChanged.connect(self.editor_tab_changed)
        self.font_box.currentFontChanged.connect(lambda _f: self.update_subtitle_preview())
        self.font_size.valueChanged.connect(lambda _v: self.update_subtitle_preview())
        self.subtitle_x.valueChanged.connect(lambda _v: self.position_overlay())
        self.subtitle_y.valueChanged.connect(lambda _v: self.position_overlay())
        self.video_view.fileDropped.connect(lambda path: self.handle_dropped_path(Path(path)))
        self.waveform.seekRequested.connect(self.player.setPosition)
        self.waveform.segmentChanged.connect(self.waveform_segment_changed)
        self.waveform.segmentSelected.connect(self.waveform_segment_selected)
        self.waveform.segmentEditStarted.connect(lambda: self.active_editor().push_undo())
        self.waveform.selectionChanged.connect(
            lambda rows: self.active_editor().select_segments(rows)
        )
        self.waveform.structureChanged.connect(self.waveform_structure_changed)
        self.waveform.segmentEditRequested.connect(self.waveform_edit_requested)
        self.waveformReady.connect(self.original_waveform_ready)
        self.vocalsWaveformReady.connect(self.vocals_waveform_ready)
        for editor in (self.pt_editor, self.en_editor, self.lyric_editor):
            editor.editingStarted.connect(
                lambda row, source=editor: self.subtitle_editing_started(source, row)
            )
            editor.editingFinished.connect(self.subtitle_editing_finished)
        self.subtitle_overlay.editingStarted.connect(self.overlay_editing_started)
        self.update_buttons()

    def update_buttons(self):
        has_video = bool(self.video)
        self.transcribe_btn.setEnabled(has_video)
        self.open_pt_btn.setEnabled(not self.busy)
        self.save_pt_btn.setEnabled(bool(self.pt))
        self.translate_btn.setEnabled(has_video)
        self.open_en_btn.setEnabled(not self.busy)
        self.save_en_btn.setEnabled(bool(self.en))
        self.lyrics_btn.setEnabled(has_video)
        self.open_lyric_btn.setEnabled(not self.busy)
        self.save_lyric_btn.setEnabled(bool(self.lyric))

    def closeEvent(self, event):
        self.discard_vocals_audio()
        shutil.rmtree(self.session_temp_dir, ignore_errors=True)
        super().closeEvent(event)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and not self.busy:
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls() and not self.busy:
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [Path(x.toLocalFile()) for x in e.mimeData().urls()]
        if paths:
            self.handle_dropped_path(paths[0])
            e.acceptProposedAction()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize and watched is self.video_view:
            QTimer.singleShot(0, self.fit_video)
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Space:
            focus = QApplication.focusWidget()
            if not isinstance(focus, (QLineEdit, QPlainTextEdit)) and not self.subtitle_overlay.editing:
                self.toggle_play()
                return True
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Delete:
            editor = self.active_editor()
            if QApplication.focusWidget() is editor.table:
                editor.delete_selected()
                return True
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Z
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            focus = QApplication.focusWidget()
            if isinstance(focus, QPlainTextEdit):
                return False
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.trigger_redo()
            else:
                self.trigger_undo()
            return True
        if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove) and event.mimeData().hasUrls():
            if not self.busy:
                event.acceptProposedAction()
            return True
        if event.type() == QEvent.Type.Drop and event.mimeData().hasUrls():
            if not self.busy:
                paths = [Path(x.toLocalFile()) for x in event.mimeData().urls()]
                if paths:
                    self.handle_dropped_path(paths[0])
                    event.acceptProposedAction()
            return True
        return super().eventFilter(watched, event)

    def video_size_changed(self, size):
        if size.width() <= 0 or size.height() <= 0:
            return
        self.video_scene.setSceneRect(0, 0, size.width(), size.height())
        self.video_item.setSize(size)
        self.subtitle_overlay.setTextWidth(size.width() * .82)
        self.position_overlay()
        self.fit_video()

    def fit_video(self):
        rect = self.video_scene.sceneRect()
        if rect.width() > 0 and rect.height() > 0:
            self.video_view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    def media_status_changed(self, status):
        if (
            status == QMediaPlayer.MediaStatus.LoadedMedia
            and self.loading_first_frame
        ):
            self.loading_first_frame = False
            self.audio.setMuted(True)
            self.player.setPosition(1)
            self.player.play()
            QTimer.singleShot(120, self.freeze_first_frame)

    def freeze_first_frame(self):
        self.player.pause()
        self.player.setPosition(0)
        self.audio.setMuted(self.user_muted)

    def toggle_mute(self):
        self.user_muted = not self.user_muted
        self.apply_mute()
        self.volume_btn.setText("🔇" if self.user_muted else "🔊")
        self.volume_btn.setToolTip("Desmutar" if self.user_muted else "Mutar")

    def apply_mute(self):
        # Só uma fonte de áudio toca por vez: a inativa fica sempre muda
        # para não sobrepor a voz isolada com o áudio original do vídeo.
        if self.isolated_active:
            self.audio.setMuted(True)
            self.vocals_audio.setMuted(self.user_muted)
        else:
            self.audio.setMuted(self.user_muted)
            self.vocals_audio.setMuted(True)

    def set_players_volume(self, value: int):
        self.audio.setVolume(value / 100)
        self.vocals_audio.setVolume(value / 100)

    def mirror_playback_state(self, state):
        if not self.isolated_active or self.vocals_path is None:
            return
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.vocals_player.play()
        else:
            self.vocals_player.pause()

    def set_vocals_path(self, path: Path | None):
        self.vocals_path = path
        self.vocals_waveform = None
        if path is None:
            self.isolated_toggle.setChecked(False)
            self.isolated_toggle.setEnabled(False)
        else:
            self.isolated_toggle.setEnabled(True)

    def new_vocals_output_path(self) -> Path:
        self.vocals_counter += 1
        return self.session_temp_dir / f"vocals_{self.vocals_counter}.wav"

    def discard_vocals_audio(self):
        # Solta o arquivo antes de apagá-lo, senão o Windows recusa por
        # ainda estar aberto no player.
        self.vocals_player.stop()
        self.vocals_player.setSource(QUrl())
        if self.vocals_path is not None:
            try:
                self.vocals_path.unlink(missing_ok=True)
            except OSError:
                pass
        self.set_vocals_path(None)

    def toggle_isolated_audio(self, checked: bool):
        self.isolated_active = bool(checked) and self.vocals_path is not None
        self.apply_mute()
        if self.isolated_active:
            source = QUrl.fromLocalFile(str(self.vocals_path))
            if self.vocals_player.source() != source:
                self.vocals_player.setSource(source)
            self.vocals_player.setPosition(self.player.position())
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.vocals_player.play()
            if self.vocals_waveform is None:
                self.load_waveform(self.vocals_path, self.vocalsWaveformReady)
            else:
                self.waveform.set_waveform(*self.vocals_waveform, preserve_view=True)
        else:
            self.vocals_player.pause()
            if self.original_waveform is not None:
                self.waveform.set_waveform(*self.original_waveform, preserve_view=True)

    def original_waveform_ready(self, peaks, duration):
        self.original_waveform = (peaks, duration)
        if not self.isolated_active:
            self.waveform.set_waveform(peaks, duration)

    def vocals_waveform_ready(self, peaks, duration):
        self.vocals_waveform = (peaks, duration)
        if self.isolated_active:
            self.waveform.set_waveform(peaks, duration, preserve_view=True)

    def update_subtitle_preview(self):
        if self.subtitle_overlay.editing:
            return
        captions = self.active_editor().captions
        seconds = self.player.position() / 1000
        text = next(
            (c["text"] for c in captions if c["start"] <= seconds < c["end"]),
            "",
        )
        text = wrap_caption(text)
        font = QFont(self.font_box.currentFont())
        # O tamanho é definido em relação a um vídeo Full HD: sem a escala,
        # a mesma fonte ficaria minúscula em vídeos verticais (1080x1920).
        rect = self.video_scene.sceneRect()
        scale = rect.height() / 1080 if rect.height() > 0 else 1.0
        font.setPixelSize(max(8, round(self.font_size.value() * scale)))
        font.setWeight(QFont.Weight.DemiBold)
        self.subtitle_overlay.setFont(font)
        safe = html.escape(text).replace("\n", "<br>")
        self.subtitle_overlay.setHtml(
            f"<div style='color:white;background-color:rgba(0,0,0,150);"
            f"padding:8px;text-align:center'>{safe}</div>"
        )
        self.subtitle_overlay.setVisible(bool(text))
        self.position_overlay()

    def position_overlay(self):
        if self.overlay_updating:
            return
        rect = self.video_scene.sceneRect()
        if rect.width() <= 0:
            return
        bounds = self.subtitle_overlay.boundingRect()
        x = rect.left() + rect.width() * self.subtitle_x.value() / 100 - bounds.width() / 2
        y = rect.top() + rect.height() * self.subtitle_y.value() / 100 - bounds.height() / 2
        x = max(rect.left(), min(x, rect.right() - bounds.width()))
        y = max(rect.top(), min(y, rect.bottom() - bounds.height()))
        self.overlay_updating = True
        self.subtitle_overlay.setPos(x, y)
        self.overlay_updating = False

    def overlay_moved(self):
        if self.overlay_updating:
            return
        rect = self.video_scene.sceneRect()
        if rect.width() <= 0:
            return
        bounds = self.subtitle_overlay.boundingRect()
        center_x = self.subtitle_overlay.x() + bounds.width() / 2
        center_y = self.subtitle_overlay.y() + bounds.height() / 2
        x = round(100 * (center_x - rect.left()) / rect.width())
        y = round(100 * (center_y - rect.top()) / rect.height())
        for widget, value in (
            (self.subtitle_x, x), (self.subtitle_x_spin, x),
            (self.subtitle_y, y), (self.subtitle_y_spin, y),
        ):
            widget.blockSignals(True)
            widget.setValue(max(0, min(100, value)))
            widget.blockSignals(False)

    def overlay_text_edited(self, text):
        editor = self.active_editor()
        editor.set_row_text(editor.table.currentRow(), text)

    def overlay_editing_finished(self):
        editor = self.active_editor()
        editor.commit_row_text(editor.table.currentRow())
        self.waveform.waveform.set_editing_index(-1)
        self.update_subtitle_preview()

    def overlay_editing_started(self):
        editor = self.active_editor()
        editor.push_undo()
        self.waveform.waveform.set_editing_index(editor.table.currentRow())

    def trigger_split(self):
        if self.subtitle_overlay.editing:
            editor = self.active_editor()
            row = editor.table.currentRow()
            text = self.subtitle_overlay.toPlainText()
            pos = self.subtitle_overlay.textCursor().position()
            if not editor.split_row(row, text[:pos], text[pos:]):
                QMessageBox.information(self, APP_NAME, "Posicione o cursor entre duas partes do texto.")
                return
            self.subtitle_overlay.stop_editing()
            return
        self.active_editor().split()

    def trigger_undo(self):
        if self.subtitle_overlay.editing:
            self.subtitle_overlay.document().undo()
            return
        if self.active_editor().undo():
            self.status.setText("Ação desfeita")

    def trigger_redo(self):
        if self.subtitle_overlay.editing:
            self.subtitle_overlay.document().redo()
            return
        if self.active_editor().redo():
            self.status.setText("Ação refeita")

    def choose_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Carregar vídeo", "", "Vídeos (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv)"
        )
        if path:
            self.load_video(Path(path))

    TRACK_LABELS = {"pt": "português", "en": "inglês", "lyric": "letra de música"}

    def open_subtitle(self, language: str):
        label = self.TRACK_LABELS[language]
        path, _ = QFileDialog.getOpenFileName(
            self, f"Abrir legenda ({label})", "", "SubRip (*.srt)"
        )
        if not path:
            return
        self.load_subtitle_path(Path(path), language)

    def load_subtitle_path(self, path: Path, language: str):
        label = self.TRACK_LABELS[language]
        try:
            captions = read_srt(path)
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, str(exc))
            return
        editor = self.editor_for(language)
        editor.set_captions(captions)
        setattr(self, language, editor.captions)
        self.show_editor(self.editors.indexOf(editor))
        if not self.video:
            self.waveform.set_waveform([], max(c["end"] for c in captions))
        self.waveform.set_captions(editor.captions)
        self.status.setText(f"Legenda ({label}) carregada: {Path(path).name}")
        self.update_buttons()

    def handle_dropped_path(self, path: Path):
        if self.busy:
            return
        if path.suffix.lower() == ".srt":
            self.load_subtitle_path(path, self.language_of(self.active_editor()))
        elif path.suffix.lower() in VIDEO_TYPES:
            self.load_video(path)
        else:
            QMessageBox.warning(
                self, APP_NAME, "Solte um arquivo de vídeo ou uma legenda SRT."
            )

    def load_video(self, path: Path):
        if self.busy:
            return
        if path.suffix.lower() not in VIDEO_TYPES:
            QMessageBox.warning(self, APP_NAME, "Selecione um arquivo de vídeo válido.")
            return
        self.video, self.pt, self.en, self.lyric = path, [], [], []
        self.pt_editor.set_captions([])
        self.en_editor.set_captions([])
        self.lyric_editor.set_captions([])
        self.show_editor(0)
        self.video_placeholder.setVisible(False)
        self.file_label.setText(f"<b>{path.name}</b><br>{path.stat().st_size / 1048576:.1f} MB")
        self.loading_first_frame = True
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.discard_vocals_audio()
        self.original_waveform = None
        self.waveform.set_captions([])
        self.waveform.set_waveform([], 1.0)
        self.load_waveform(path, self.waveformReady)
        self.update_buttons()

    def load_waveform(self, path: Path, ready_signal):
        def worker():
            try:
                proc = subprocess.run(
                    [
                        "ffmpeg", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000",
                        "-f", "s16le", "-",
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if proc.returncode or not proc.stdout:
                    return
                samples = array("h")
                samples.frombytes(proc.stdout)
                bucket = 160
                peaks = [
                    max((abs(v) for v in samples[i:i + bucket]), default=0) / 32768
                    for i in range(0, len(samples), bucket)
                ]
                ready_signal.emit(peaks, len(samples) / 8000)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def position_changed(self, pos):
        self.seek_slider.setValue(pos)
        self.time_label.setText(f"{clock(pos)} / {clock(self.player.duration())}")
        self.waveform.set_position(pos / 1000)
        self.update_subtitle_preview()
        self.active_editor().follow_position(pos / 1000)
        if self.isolated_active and abs(self.vocals_player.position() - pos) > 150:
            self.vocals_player.setPosition(pos)

    def track_editors(self):
        return {"pt": self.pt_editor, "en": self.en_editor, "lyric": self.lyric_editor}

    def editor_for(self, language: str):
        return self.track_editors()[language]

    def language_of(self, editor) -> str:
        return next(k for k, v in self.track_editors().items() if v is editor)

    def store_captions(self, editor):
        setattr(self, self.language_of(editor), editor.captions)

    def show_editor(self, index):
        self.editors.setCurrentIndex(index)
        editor = self.editors.widget(index)
        self.waveform.set_captions(editor.captions)
        self.update_subtitle_preview()

    def active_editor(self):
        return self.editors.currentWidget()

    def editor_tab_changed(self, _index):
        editor = self.active_editor()
        self.waveform.set_captions(editor.captions)
        rows = sorted({item.row() for item in editor.table.selectedIndexes()})
        self.waveform.waveform.set_selected(rows)
        self.update_subtitle_preview()

    def editor_changed(self):
        editor = self.active_editor()
        if self.editors.currentWidget() is editor:
            self.waveform.set_captions(editor.captions)
        self.store_captions(editor)
        self.update_subtitle_preview()

    def editor_selection_changed(self, editor, rows):
        if editor is self.active_editor():
            self.waveform.waveform.set_selected(rows)

    def subtitle_editing_started(self, editor, row: int):
        if editor is self.active_editor():
            self.waveform.waveform.set_editing_index(row)

    def subtitle_editing_finished(self):
        self.waveform.waveform.set_editing_index(-1)

    def waveform_segment_changed(self, row: int):
        editor = self.active_editor()
        editor.update_timing(row)
        self.store_captions(editor)
        self.update_subtitle_preview()

    def waveform_segment_selected(self, row: int):
        self.active_editor().select_segment(row)

    def waveform_edit_requested(self, row: int):
        self.active_editor().edit_segment(row)

    def waveform_structure_changed(self, selected_rows):
        editor = self.active_editor()
        target = selected_rows[0] if selected_rows else 0
        editor.refresh(target)
        editor.select_segments(selected_rows)
        self.store_captions(editor)
        editor.changed.emit()

    def run_job(self, operation):
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.set_busy(True)
        thread = QThread(self)
        job = Job(operation)
        job.moveToThread(thread)
        thread.started.connect(job.run)
        job.progress.connect(lambda text, value: (self.status.setText(text), self.progress.setValue(value)))
        job.failed.connect(self.job_failed)
        job.finished.connect(self.job_done)
        job.finished.connect(thread.quit)
        job.failed.connect(thread.quit)
        thread.finished.connect(job.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.thread, self.job = thread, job
        # Prioridade baixa: trabalho pesado (separação de voz, Whisper) não
        # deve competir pela CPU com a thread da interface e a decodificação
        # de vídeo, o que pode desestabilizar o Qt durante o processamento.
        thread.start(QThread.Priority.LowPriority)

    def job_failed(self, message):
        self.set_busy(False)
        self.progress.setVisible(False)
        self.status.setText("Não foi possível concluir")
        QMessageBox.critical(self, APP_NAME, message)

    def job_done(self, payload):
        self.set_busy(False)
        self.progress.setVisible(False)
        kind, result = payload
        messages = {
            "pt": "Transcrição pronta para revisão",
            "en": "Tradução pronta para revisão",
            "lyric": "Letra sincronizada pronta para revisão",
        }
        if kind == "lyric":
            captions, vocals_path = result
            self.set_vocals_path(vocals_path)
        else:
            captions = result
        setattr(self, kind, captions)
        editor = self.editor_for(kind)
        editor.set_captions(captions)
        self.show_editor(self.editors.indexOf(editor))
        self.status.setText(messages[kind])
        self.update_buttons()

    def set_busy(self, busy: bool):
        self.busy = busy
        if busy:
            for button in (
                self.transcribe_btn, self.open_pt_btn, self.save_pt_btn,
                self.translate_btn, self.open_en_btn, self.save_en_btn,
                self.lyrics_btn, self.open_lyric_btn, self.save_lyric_btn,
            ):
                button.setEnabled(False)
        else:
            self.update_buttons()

    def transcribe(self):
        if not ensure_model(self):
            self.status.setText("O modelo precisa ser baixado antes da transcrição")
            return
        video = self.video
        self.run_job(lambda emit: ("pt", Engine(emit).transcribe(video)))

    def translate(self):
        if not ensure_model(self):
            self.status.setText("O modelo precisa ser baixado antes da tradução")
            return
        video = self.video
        self.run_job(lambda emit: ("en", Engine(emit).translate(video)))

    def sync_lyrics(self):
        if not ensure_model(self):
            self.status.setText("O modelo precisa ser baixado antes da sincronização")
            return
        dialog = LyricsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        video, lyrics = self.video, dialog.lyrics
        self.discard_vocals_audio()
        vocals_output = self.new_vocals_output_path()
        self.run_job(
            lambda emit: ("lyric", Engine(emit).sync_lyrics(video, lyrics, vocals_output))
        )

    def save(self, language):
        editor = self.editor_for(language)
        editor.commit_text()
        captions = [dict(x) for x in editor.captions]
        setattr(self, language, captions)
        suffix = {"pt": "pt", "en": "en", "lyric": "letra"}[language]
        default = self.video.with_name(f"{self.video.stem}.{suffix}.srt")
        path, _ = QFileDialog.getSaveFileName(self, "Salvar legenda", str(default), "SubRip (*.srt)")
        if path:
            if not path.lower().endswith(".srt"):
                path += ".srt"
            write_srt(Path(path), captions)
            self.status.setText(f"Salvo: {path}")


def make_app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        gradient = QLinearGradient(0, 0, size, size)
        gradient.setColorAt(0, QColor("#ff5c81"))
        gradient.setColorAt(1, QColor("#a3204a"))
        p.setBrush(QBrush(gradient))
        p.setPen(Qt.PenStyle.NoPen)
        radius = size * .22
        p.drawRoundedRect(0, 0, size, size, radius, radius)
        p.setBrush(QColor(255, 255, 255, 235))
        play = QPolygonF([
            QPointF(size * .38, size * .18),
            QPointF(size * .68, size * .35),
            QPointF(size * .38, size * .52),
        ])
        p.drawPolygon(play)
        bar_h = max(1.5, size * .10)
        p.drawRoundedRect(int(size * .16), int(size * .62), int(size * .68), int(bar_h), bar_h / 2, bar_h / 2)
        p.drawRoundedRect(int(size * .28), int(size * .78), int(size * .44), int(bar_h), bar_h / 2, bar_h / 2)
        p.end()
        icon.addPixmap(pm)
    return icon


if __name__ == "__main__":
    if os.name == "nt":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DinCreation.DinSubtitler")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(make_app_icon())
    if "--portable-self-test" in sys.argv:
        print(f"root={ROOT}")
        print(f"components={BIN_DIR}")
        print(f"ffmpeg={shutil.which('ffmpeg') or 'missing'}")
        print(f"model={'ready' if model_is_ready() else 'missing'}")
        sys.exit(0 if shutil.which("ffmpeg") else 2)
    window = MainWindow()
    window.show()
    if not model_is_ready():
        QTimer.singleShot(250, lambda: ensure_model(window))
    sys.exit(app.exec())
