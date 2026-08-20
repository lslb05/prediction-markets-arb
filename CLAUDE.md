# Contrato do projeto — Prediction Markets Arb

Leia o `CLAUDE.md` global e use este arquivo para o que é específico deste
repositório.

## Resultado do produto

- **Problema:** Kalshi e Polymarket listam contratos sobre os mesmos eventos e
  cotam de forma independente, sem identificador comum, sem taxonomia comum e
  sem redação comum. Descobrir quais pares são de fato o mesmo contrato é o
  trabalho; a aritmética da arbitragem é trivial depois disso.
- **Usuário:** eu. A série de spreads gravada é a saída de pesquisa; o
  `trader/` é a parte que age sobre ela.
- **Saída verificável:** `core.arbitrage_scans` responde com dado, e não de
  memória, quantas vezes um edge apareceu e quão largo era. `prediction-markets
  opportunities --hours N` imprime isso em CSV.
- **Fonte de dados permitida:** Gamma API e CLOB da Polymarket, trade API v2 da
  Kalshi. Ambas públicas e gratuitas. A Kalshi exige par de chaves, não conta
  com saldo, para leitura.
- **Menor corte ponta a ponta:** ingestão dos dois catálogos → embedding →
  candidatos rerankeados → confirmação humana → varredura de spread gravada.
  Entregue.
- **Fora de escopo:** outras casas além dessas duas; sizing e construção de
  portfólio; qualquer coisa parecida com backtest, já que a série só começa
  quando o scanner começa.

## Comandos

```bash
uv sync
(cd trader && npm ci)
uv run python scripts/format.py
uv run python scripts/check.py    # roda as duas metades
```

## Regras locais

- O pacote Python é `prediction_markets` e vive em `src/prediction_markets`.
  O nome do repositório tem o sufixo `-arb`; o pacote não.
- `sources/` fala com a casa e valida; `ingestion/` orquestra adapter e banco.
  Mudam por motivos diferentes, então são arquivos diferentes.
- **Todo SQL do pacote está em `repository.py`.** Nada acima dele escreve SQL,
  e ele não conhece HTTP nem modelo.
- `arbitrage/pricing.py` e `trader/src/strategy/spread.ts` são a mesma função
  em duas linguagens. Mexer numa sem a outra é defeito mesmo com as duas
  suítes passando — os dois testes afirmam os mesmos números calculados à mão,
  e é isso que segura.
- `Encoder` e `Reranker` são Protocol. Nada acima de `matching/encoders.py`
  importa torch, e é por isso que o CI exercita o pipeline inteiro sem baixar
  3 GB de checkpoint. Não introduza import direto de `sentence_transformers`
  fora das funções de carga.
- **`TRADER_LIVE` é a única coisa entre o bot e dinheiro real.** A trava vive
  dentro de `KalshiApi` e `PolymarketExecutor`, não no engine. Não mova, não
  contorne, não adicione um segundo caminho de execução.
- Nenhum `.pem` no repositório. `secrets/` inteiro é ignorado: a Kalshi emite
  a chave uma vez e não deixa baixar de novo.
- O schema é aplicado a cada conexão (`postgres.connect`), todo statement é
  `IF NOT EXISTS` ou `OR REPLACE`. Não há passo de migração; se um dia for
  preciso mudança destrutiva, isso vira uma decisão em `docs/decisions.md`.
- Configuração vem de ambiente por `Settings`.
- Preserve dado bruto: `raw.write` guarda o corpo original com manifesto e
  hash antes de qualquer interpretação.
- Atualize `docs/decisions.md` quando uma decisão estrutural mudar, incluindo
  o que a tornaria errada.

## Estilo

Código público, lido por gente. A regra é contenção — a lista completa está em
`AGENTS.md`, seção "Style", e vale igual para mim e para o Codex.

- Comentário explica *por quê*, nunca *o quê*. Se parafraseia a linha abaixo,
  apague; se a linha precisa de paráfrase, renomeie a variável.
- Docstring só onde a assinatura não basta (unidade, fuso, efeito colateral,
  o que levanta). Helper privado de três linhas não leva.
- Teste afirma comportamento que pode quebrar, não constante. Cobertura é
  consequência de testar o que importa, nunca meta a inflar.
- Não capture exceção que não sabe tratar; não abstraia com um único chamador.
- Sem emoji e sem log em tom de narração — inclusive no `trader/`, que nasceu
  com um dashboard emoji que se sobrescrevia e não deixava registro.

## Autoria dos commits

Autor e committer são um só: o dono do repositório. Nenhum `Co-Authored-By:`,
nenhum trailer de sessão, nenhuma menção a Claude, Codex ou ferramenta. Não
altere `user.name` nem `user.email`.

## Estado da sessão

- **Decidido:** portado do repositório exploratório `PredictionMarkets` e do
  banco em `Database`. Os dois lados — pipeline Python e bot TypeScript —
  estão no mesmo repositório, com um único portão de qualidade.
- **Pendente:** profundidade de book na precificação (hoje só o topo), e uma
  medida de precisão do matching (hoje a fila humana é o único controle).
- **Próximo passo:** rodar o pipeline com credencial real e chegar ao primeiro
  par confirmado a partir do catálogo de verdade, não da fixture.
