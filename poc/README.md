# POC — validação de meia-entrada de estudante (CIE)

Prova de conceito do fluxo completo: código da carteirinha entra, decisão de
elegibilidade sai, passando pelo gateway MCP até as APIs de produção dos validadores
da CIE: a base DNE (UNE/UBES/ANPG) e a FESN.

## Rodar em 30 segundos (sem credencial, sem rede)

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python poc/validar_cie.py --demo
```

O modo demo roda a mesma função `cie.check` da consulta real (monta o payload, confere o
status HTTP e a validade, interpreta o `codigoRetorno`), mas no lugar da chamada de rede
entrega respostas gravadas da API real: carteirinha válida, inexistente e payload fora do
contrato. Gateway, credencial, rede e a base FESN **não** passam pelo demo. A data do
evento é fixa (2026-12-20), para o resultado não depender do relógio.

Com `--json`, a saída é um único documento JSON (um objeto por caso); o aviso do demo vai
para o stderr.

## Rodar de verdade

Precisa de duas coisas: um **código de acesso de estabelecimento** e uma **CIE real**.

```bash
export MEIAENTRADA_CODIGO_ACESSO=...        # cadastro em meiaentrada.org.br/validador/integre
.venv/bin/python poc/validar_cie.py \
  --codigo-uso ABC123 \
  --nascimento 1999-04-21 \
  --evento "Show X" \
  --cnpj 00.000.000/0001-91
```

Só o DNE (`--provedor auto`, o padrão, e `--provedor dne`) pede `--nascimento` e `--cnpj`.
Para consultar apenas a FESN, que não pede credencial, basta o código de uso:

```bash
.venv/bin/python poc/validar_cie.py --provedor fesn --codigo-uso AB12CD34 --data-evento 2026-12-20
```

`--data-evento` vale para todos os provedores: a validade da carteirinha é conferida contra
a data do evento, não contra a da compra. Sem ele, usa o dia de hoje em America/Sao_Paulo.

### Exit codes

| Código | Significado | O que fazer |
|---|---|---|
| 0 | elegível | liberar a meia-entrada e conferir foto e documento na portaria |
| 1 | não elegível: um validador respondeu e a carteirinha não vale (vencida para a data do evento, nascimento que não confere, não encontrada nas bases consultadas) | tratar como pendência documental, não como recusa automática |
| 2 | entrada inválida ou credencial ausente | corrigir a chamada: argumento faltando ou mal formado, ou `MEIAENTRADA_CODIGO_ACESSO` ausente com `--provedor dne` |
| 3 | não validado: nenhum validador respondeu (erro de rede ou do gateway, HTTP fora de 2xx, resposta truncada ou ilegível) | tentar de novo; se persistir, pedir comprovação documental |

Com `--json` a saída serve direto para pipeline; o campo `validated` é `false` no exit 3.
As regras por trás desses códigos estão em [../docs/meia-entrada.md](../docs/meia-entrada.md).

O `codigoUso` e a `dataNascimento` são exatamente os dois valores que a URL do QR code
da carteirinha carrega (`/validador/:codigoUso/:dataNascimento`) — em portaria, ler o QR
já dá os dois.

## O que a API devolve quando aprova

`nome`, `nomeSocial`, `cpf`, `documento`, `instituicao`, `curso`, `nivelEscolaridade`,
`entidade`, `municipio`, `certificado`, `dataExpiracao` e a foto (`urlFoto`/`foto`).
Ou seja: dá para conferir titularidade na hora, comparando foto e documento com a pessoa.

## Armadilhas do validador (custaram tempo, ficam registradas)

| Sintoma | Causa real |
|---|---|
| `codigoRetorno: 5`, "parâmetros inválidos" | payload fora do contrato — o campo é `codigoUso`, **não** `certificado` (esse só existe na resposta) |
| `codigoRetorno: 2`, "documento inexistente" | **ambíguo**: mesma resposta para código inexistente e para carteirinha válida com data de nascimento errada (confirmado com carteirinha real). Na UX, mande conferir os dois campos — nunca diga "carteirinha inválida" |
| HTTP 500 | `dataNascimento` fora de `YYYYMMDD` (ex.: DDMMYYYY derruba o servidor) |
| HTTP 200 com `status: false` | **toda** falha vem como 200; nunca trate código HTTP como veredito |
| `status` às vezes é a string `"true"` | o próprio front oficial testa `=== "true" || === true` |
| `/public/cie/status` responde 404 | endpoint morto que ainda aparece no bundle do site |

A camada `src/govbr_apis_mcp/cie.py` isola tudo isso: normaliza datas (aceita `YYYYMMDD`,
`YYYY-MM-DD` e `DD/MM/YYYY`, só datas que existem no calendário), confere que o CNPJ tem
14 dígitos (não valida os dígitos verificadores) e que o código de uso foi informado antes
de chamar, e traduz `codigoRetorno` em motivo legível.

## Múltiplas emissoras — e por que isso não é detalhe

A POC consulta duas bases antes de negar (`--provedor auto`, padrão):

| Base | Contrato | Auth | Data de nascimento |
|---|---|---|---|
| DNE (UNE/UBES/ANPG) | `POST /public/validador` | header `codigoAcesso` | obrigatória |
| FESN | `GET /api/students/usecode/{codigo}` | nenhuma | não aceita |

Isso não é hipótese: no primeiro teste com carteirinhas reais, uma delas (IFRS, válida
até 2027) **não existe na base DNE** e só validou na FESN. Se o fluxo tratasse a primeira
falha como "não tem direito", teria negado meia-entrada a quem tem direito por lei.

**A FESN é fonte de dado pessoal chamável sem credencial.** Quem souber um código de uso
recebe nome, CPF, data de nascimento e foto do portador, sem chave a proteger. Por isso o
catálogo marca `fesn-cie-validador` com `pii: true`. Numa decisão não elegível nada pessoal
é devolvido: `holder` fica vazio e `raw` só leva `status`, `codigoRetorno`, `mensagem`, a
validade e, se ninguém respondeu, o diagnóstico do gateway. Os dados do portador só aparecem
quando a carteirinha é aprovada, para a conferência na portaria.

A FESN não valida expiração nem titularidade — devolve `validity` sem aplicá-la, e o
código de uso sozinho libera a consulta. `interpret_fesn` faz as checagens por nós: exige
que a resposta seja da carteirinha consultada (mesmo `useCode`), recusa carteirinha vencida
para a data do evento ou com validade ilegível e, quando a data de nascimento é informada,
confere contra a que veio no payload. O HTTP 404 é lido como "não encontrada" (a
resposta esperada para código desconhecido, com um objeto de erro no corpo); qualquer outro
status fora de 2xx significa "não validado".

Ainda assim a cobertura é parcial: DCEs, CAs e outras entidades estaduais emitem CIE
legítima e nem todas têm API. **`não validou` significa `pendência documental`, nunca
`recusado`** — é a regra de produto que a POC codifica na mensagem de erro e no exit 3.

Para PcD e professor não existe rota consultável por empresa privada; ID Jovem só valida
manualmente. O levantamento completo está em [../docs/meia-entrada.md](../docs/meia-entrada.md).

## Testes

```bash
.venv/bin/pytest tests/test_cie.py tests/test_poc_cli.py -v
```

A suíte em `tests/test_cie.py` cobre a camada de elegibilidade: datas, contrato do payload,
cada código de retorno, validade contra a data do evento, os dois validadores e os modos de
falha do gateway. A de `tests/test_poc_cli.py` cobre a CLI: exit codes de cada provedor e
`--demo --json`. Nenhum teste toca a rede nem depende do relógio.
