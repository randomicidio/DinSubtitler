# Din Subtitler

O **Din Subtitler** é um editor de legendas para Windows que transcreve e traduz vídeos localmente com o Whisper. Ele reúne player de vídeo, waveform do áudio, ajuste visual de tempos e edição de texto em uma única interface.

Todo o processamento acontece no computador. Os vídeos não são enviados para serviços externos e o uso não consome créditos de API.

## Duas ferramentas independentes

O programa oferece dois processos distintos. Depois de carregar um vídeo, você escolhe qual deles deseja executar:

### Transcrição em português

O Whisper reconhece a fala do vídeo e cria uma faixa de legendas em português, com texto e tempos próprios.

### Tradução para inglês

O Whisper escuta diretamente o áudio em português e cria uma faixa de legendas traduzida para o inglês. Esse processo é independente da transcrição em português: não é necessário transcrever ou revisar a faixa PT antes de traduzir.

As duas faixas ficam disponíveis em abas separadas. Você pode editar, juntar, separar, excluir e ajustar os tempos de cada idioma independentemente. Quando terminar, pode salvar a faixa portuguesa como `.pt.srt` e a inglesa como `.en.srt`.

## Principais recursos

- Transcrição de áudio em português com Whisper `large-v3`
- Tradução direta do áudio em português para inglês
- Processamento local e privado
- Aceleração por GPU NVIDIA, com fallback para CPU
- Player de vídeo com prévia das legendas
- Edição de texto diretamente na lista ou sobre o vídeo
- Waveform navegável e sincronizada com a reprodução
- Ajuste visual do início, fim e posição de cada trecho
- Controle de fonte, tamanho e posição da legenda na prévia
- Exportação independente das legendas PT e EN em formato SRT

## Instalação

1. Baixe ou clone este repositório.
2. Abra `Instalar componentes.bat`.
3. Aguarde a instalação do FFmpeg, das dependências Python e do suporte à GPU.
4. Abra `Din Subtitler.bat`.

O modelo Whisper será baixado automaticamente na primeira utilização e ficará dentro da pasta `models`.

## Fluxo de uso

1. Arraste um vídeo para a janela ou clique em **Carregar vídeo**.
2. Escolha **Transcrever para português**, **Traduzir para inglês** ou execute os dois processos.
3. Abra a aba do idioma que deseja revisar.
4. Edite o texto e os tempos livremente.
5. Salve o SRT correspondente ao idioma.

## Controles do editor

- `Espaço`: reproduzir ou pausar o vídeo
- `F4`: juntar dois trechos consecutivos selecionados
- `F5`: separar um trecho na posição do cursor
- `Delete`: excluir os trechos selecionados
- `Shift + Enter`: inserir uma quebra de linha manual
- Rodinha do mouse sobre a waveform: aplicar zoom
- `Alt + rodinha`: navegar horizontalmente pela waveform

Na waveform:

- Arraste a linha de reprodução para navegar pelo vídeo.
- Arraste um bloco para mover o trecho inteiro.
- Arraste as bordas para ajustar o início ou o fim.
- Arraste a divisão entre blocos adjacentes para alterar os dois tempos simultaneamente.

## Privacidade e funcionamento offline

Depois da instalação inicial e do download do modelo, o Din Subtitler pode funcionar sem internet. Nenhum vídeo, áudio ou texto é enviado para terceiros.
