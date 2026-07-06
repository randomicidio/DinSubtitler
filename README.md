# Din Subtitler

O **Din Subtitler** é um editor de legendas para Windows que transcreve e traduz vídeos localmente com o Whisper. Ele reúne player de vídeo, waveform do áudio, ajuste visual de tempos e edição de texto em uma única interface.

Todo o processamento acontece no computador. Os vídeos não são enviados para serviços externos e o uso não consome créditos de API.

## Três ferramentas independentes

O programa oferece três processos distintos. Depois de carregar um vídeo, você escolhe qual deles deseja executar:

### Transcrição em português

O Whisper reconhece a fala do vídeo e cria uma faixa de legendas em português, com texto e tempos próprios.

### Tradução para inglês

O Whisper escuta diretamente o áudio em português e cria uma faixa de legendas traduzida para o inglês. Esse processo é independente da transcrição em português: não é necessário transcrever ou revisar a faixa PT antes de traduzir.

### Letra de música sincronizada

Para vídeos de alguém cantando uma música. Você informa o artista e o nome da música, o programa busca a letra oficial na internet (via [LRCLIB](https://lrclib.net)) e mostra o texto para revisão — também é possível colar a letra manualmente. Ao sincronizar, a voz é isolada do instrumental com um modelo de separação vocal (MDX-Net, baixado uma única vez, ~64 MB), o Whisper escuta a voz isolada e cada verso da letra é posicionado no momento em que é cantado. Versos que o áudio não deixou claros são posicionados pela presença de voz no intervalo e podem ser ajustados na waveform.

As faixas ficam disponíveis em abas separadas. Você pode editar, juntar, separar, excluir e ajustar os tempos de cada uma independentemente. Quando terminar, pode salvar a faixa portuguesa como `.pt.srt`, a inglesa como `.en.srt` e a letra como `.letra.srt`.

## Principais recursos

- Transcrição de áudio em português com Whisper `large-v3`
- Tradução direta do áudio em português para inglês
- Sincronização automática de letras de música com o áudio do vídeo
- Processamento local e privado
- Aceleração por GPU NVIDIA, com fallback para CPU
- Player de vídeo com prévia das legendas
- Edição de texto diretamente na lista ou sobre o vídeo
- Importação de arquivos SRT em português ou inglês para continuar uma edição
- Waveform navegável e sincronizada com a reprodução
- Ajuste visual do início, fim e posição de cada trecho
- Controle de fonte, tamanho e posição da legenda na prévia
- Exportação independente das legendas PT e EN em formato SRT

## Instalação

### Versão portable

1. Baixe e extraia a pasta do Din Subtitler.
2. Abra `Din Subtitler.exe`.
3. Na primeira abertura, clique em **Baixar componentes**.

O programa não precisa ser instalado no Windows. O modelo Whisper será salvo em
`models\whisper`, dentro da própria pasta portable. Depois do primeiro download,
basta abrir o mesmo EXE.

Em computadores com placa NVIDIA, a janela inicial também oferece a aceleração
por GPU. Esses arquivos opcionais ficam em `components\nvidia`.

Os arquivos `.bat` e o ambiente Python são necessários apenas para quem deseja
executar ou modificar o código-fonte.

## Fluxo de uso

1. Arraste um vídeo para qualquer região da janela ou clique em **Carregar vídeo**.
2. Escolha **Transcrever para português**, **Traduzir para inglês** ou **Sincronizar letra de música**.
   Você também pode abrir um SRT existente diretamente na seção correspondente.
3. Abra a aba da faixa que deseja revisar.
4. Edite o texto e os tempos livremente.
5. Salve o SRT correspondente à faixa.

## Controles do editor

- `Espaço`: reproduzir ou pausar o vídeo
- Clique no ícone de volume para mutar ou desmutar
- Arraste o volume para ver o valor; dê duplo clique para voltar a 80%
- `Ctrl + Z`: desfazer a última ação na faixa selecionada
- `Ctrl + Shift + Z`: refazer a última ação desfeita
- `↑` / `↓`: navegar entre os trechos sem entrar no modo de edição
- `Enter` ou duplo clique: editar o texto do trecho selecionado
- `F4`: juntar dois trechos consecutivos selecionados
- `F5`: separar um trecho na posição do cursor
- `S`: separar o trecho sob o cursor de reprodução, dividindo o texto de forma inteligente pela pontuação mais próxima do meio
- `X`: separar o trecho sob o cursor de reprodução mantendo o texto completo nas duas metades
- `Delete`: excluir os trechos selecionados
- `Shift + Enter`: inserir uma quebra de linha manual
- Rodinha do mouse sobre a waveform: aplicar zoom
- Arrastar com a rodinha pressionada: navegar horizontalmente pela waveform
- Duplo clique no controle X: restaurar a posição horizontal para 50%

Na waveform:

- Clique ou arraste com o botão esquerdo numa área vazia para navegar pelo vídeo.
- Arraste com o botão direito sobre uma área vazia para selecionar vários blocos.
- Segure `Ctrl` e arraste em uma área vazia para desenhar um novo bloco.
- Segure `Ctrl` e clique em blocos para adicioná-los ou removê-los da seleção.
- Selecione um ou mais blocos, segure `Alt` e arraste para criar uma cópia.
- Dê duplo clique num bloco para editar seu texto na lista.
- Arraste um bloco para mover o trecho inteiro.
- Com vários blocos selecionados, arraste qualquer um deles para mover o grupo inteiro.
- Arraste as bordas para ajustar o início ou o fim.
- Arraste a divisão entre blocos adjacentes para alterar os dois tempos simultaneamente.

Blocos criados ou duplicados têm prioridade sobre os anteriores. Quando existe
sobreposição, os blocos antigos são recortados automaticamente para que duas
legendas nunca ocupem o mesmo intervalo.

## Privacidade e funcionamento offline

Depois da instalação inicial e do download do modelo, o Din Subtitler pode funcionar sem internet. Nenhum vídeo, áudio ou texto é enviado para terceiros.

A única exceção é a busca de letras de música, que consulta o serviço LRCLIB pela internet enviando apenas o nome do artista e da música. O áudio do vídeo continua sendo processado localmente, e você pode colar a letra manualmente para usar o recurso sem internet.
