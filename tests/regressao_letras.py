# -*- coding: utf-8 -*-
"""Regressão da sincronização de letras contra correções manuais reais.

Cada pasta em tests/dados/CasoN guarda um caso de estudo:
  - words_*.json    palavras ouvidas pelo Whisper (voz isolada com/sem VAD, mix)
  - env.npy         envelope de energia da voz isolada
  - Musica.txt      artista, música e a letra usada na sincronização
  - corrigido.srt   os tempos corrigidos manualmente (gabarito)

O teste reexecuta só a parte determinística do pipeline (alinhamento,
distribuição e refino) a partir desses caches — sem GPU, ffmpeg ou vídeos —
e mede a distância até o gabarito. Rode após qualquer mudança nas funções
de letra do app_v2:

    .venv\\Scripts\\python.exe tests\\regressao_letras.py       (resumo)
    .venv\\Scripts\\python.exe tests\\regressao_letras.py -v    (verso a verso)

Números de referência (2026-07-08, depois das correções de envelope):
  TOTAL: |dS| 0.51s  |dE| 0.53s  0 fantasmas  0 perdidos  20 erros >1s
Antes das correções o mesmo conjunto media 1.10s/1.14s com 7 fantasmas.
Uma mudança boa mantém ou reduz esses números; fantasmas e perdidos devem
continuar em zero.
"""
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DADOS = Path(__file__).resolve().parent / "dados"
sys.path.insert(0, str(ROOT))

import app_v2 as app  # noqa: E402


def parse_srt(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    entries = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", lines[1])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        entries.append({
            "start": g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000,
            "end": g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000,
            "text": " ".join(lines[2:]),
        })
    return entries


def norm(t):
    return re.sub(r"\s+", " ", t.lower()).strip()


def case_lyrics(case_dir: Path) -> str:
    text = (case_dir / "Musica.txt").read_text(encoding="utf-8-sig")
    parts = text.split("\n\n", 1)  # a letra vem depois do cabeçalho
    return parts[1] if len(parts) > 1 else text


def run_case(case_dir: Path):
    """Reproduz o fluxo do Engine.sync_lyrics a partir dos caches."""
    lines = app.lyric_lines(case_lyrics(case_dir))
    env = np.load(case_dir / "env.npy")

    def load(tag):
        p = case_dir / f"words_{tag}.json"
        if not p.exists():
            return [], None
        d = json.loads(p.read_text(encoding="utf-8"))
        return d["words"], d["total"]

    vocal_words, total = load("vocal")
    vocal_raw_words, t2 = load("vocal_raw")
    mix_words, t3 = load("mix")
    total = total or t2 or t3

    def cov(*sources):
        spans = None
        for source in sources:
            if not source:
                continue
            cur = app.align_lyrics_to_words(lines, source, env)
            spans = cur if spans is None else app.merge_spans(spans, cur)
        return 0.0 if spans is None else sum(1 for s in spans if s) / len(lines)

    # replica a decisão de passes extras da produção
    use_raw = cov(vocal_words) < 0.92
    use_mix = cov(vocal_words, vocal_raw_words if use_raw else []) < 0.92

    spans = app.align_lyrics_to_words(lines, vocal_words, env)
    for support, used in ((vocal_raw_words, use_raw), (mix_words, use_mix)):
        if support and used:
            spans = app.merge_spans(spans, app.align_lyrics_to_words(lines, support, env))

    all_words = sorted(
        vocal_words
        + (vocal_raw_words if use_raw else [])
        + (mix_words if use_mix else []),
        key=lambda w: w["start"],
    )
    captions = app.captions_from_lyrics(lines, deepcopy(spans), total, env, words=all_words)
    captions = app.refine_lyric_starts(captions, env)
    captions = app.extend_lyric_ends(captions, total, env)
    return captions


def evaluate(gen, fix):
    """Pareia sequencialmente por texto e mede as diferenças."""
    pairs, ghosts = [], []
    j = 0
    for g in gen:
        found = next(
            (k for k in range(j, min(j + 3, len(fix)))
             if norm(fix[k]["text"]) == norm(g["text"])),
            None,
        )
        if found is None:
            ghosts.append(g)
        else:
            pairs.append((g, fix[found]))
            j = found + 1
    ds = [abs(f["start"] - g["start"]) for g, f in pairs]
    de = [abs(f["end"] - g["end"]) for g, f in pairs]
    return {
        "pares": len(pairs),
        "fantasmas": ghosts,
        "perdidos": len(fix) - len(pairs),
        "ds": ds,
        "de": de,
        "pairs": pairs,
    }


def main():
    verbose = "-v" in sys.argv
    tot_ds, tot_de, tot_n = 0.0, 0.0, 0
    tot_fant = tot_perd = tot_big = 0
    print(f"{'caso':<8}{'pares':>6}{'fantasmas':>10}{'perdidos':>9}"
          f"{'|dS| méd':>10}{'|dE| méd':>10}{'>1s':>5}")
    for case_dir in sorted(DADOS.glob("Caso*")):
        gen = run_case(case_dir)
        fix = parse_srt(case_dir / "corrigido.srt")
        m = evaluate(gen, fix)
        ds_med = sum(m["ds"]) / len(m["ds"]) if m["ds"] else 0.0
        de_med = sum(m["de"]) / len(m["de"]) if m["de"] else 0.0
        big = sum(1 for x in m["ds"] + m["de"] if x > 1.0)
        print(f"{case_dir.name:<8}{m['pares']:>6}{len(m['fantasmas']):>10}"
              f"{m['perdidos']:>9}{ds_med:>10.2f}{de_med:>10.2f}{big:>5}")
        if verbose:
            for g, f in m["pairs"]:
                ds, de = f["start"] - g["start"], f["end"] - g["end"]
                if abs(ds) > 1.0 or abs(de) > 1.0:
                    print(f"    {g['text'][:40]:<42} dS={ds:+.2f} dE={de:+.2f}"
                          f"  gen[{g['start']:.2f}-{g['end']:.2f}]"
                          f" corr[{f['start']:.2f}-{f['end']:.2f}]")
            for g in m["fantasmas"]:
                print(f"    FANTASMA [{g['start']:.2f}-{g['end']:.2f}] {g['text'][:40]}")
        tot_ds += ds_med * m["pares"]
        tot_de += de_med * m["pares"]
        tot_n += m["pares"]
        tot_fant += len(m["fantasmas"])
        tot_perd += m["perdidos"]
        tot_big += big
    print(f"{'TOTAL':<8}{tot_n:>6}{tot_fant:>10}{tot_perd:>9}"
          f"{tot_ds / tot_n:>10.2f}{tot_de / tot_n:>10.2f}{tot_big:>5}")


if __name__ == "__main__":
    main()
