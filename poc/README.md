# POC — validação de meia-entrada de estudante (CIE)

Prova de conceito do fluxo completo: código da carteirinha entra, decisão de
elegibilidade sai, passando pelo gateway MCP até a API de produção do validador
oficial da CIE (base DNE — UNE/UBES/ANPG).

## Rodar em 30 segundos (sem credencial, sem rede)

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python poc/validar_cie.py --demo
```

O modo demo percorre os mesmos caminhos de código com respostas gravadas da API real:
carteirinha válida, inexistente e payload fora do contrato.

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

Exit code 0 = elegível, 1 = não elegível, 2 = entrada inválida. Com `--json` a saída
serve direto para pipeline.

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

A camada `src/govbr_apis_mcp/cie.py` isola tudo isso: normaliza data (aceita
`YYYY-MM-DD`, `DD/MM/YYYY`), valida CNPJ e código antes de chamar, e traduz
`codigoRetorno` em motivo legível.

## Múltiplas emissoras — e por que isso não é detalhe

A POC consulta duas bases antes de negar (`--provedor auto`, padrão):

| Base | Contrato | Auth | Data de nascimento |
|---|---|---|---|
| DNE (UNE/UBES/ANPG) | `POST /public/validador` | header `codigoAcesso` | obrigatória |
| FESN | `GET /api/students/usecode/{codigo}` | nenhuma | não aceita |

Isso não é hipótese: no primeiro teste com carteirinhas reais, uma delas (IFRS, válida
até 2027) **não existe na base DNE** e só validou na FESN. Se o fluxo tratasse a primeira
falha como "não tem direito", teria negado meia-entrada a quem tem direito por lei.

A FESN não valida expiração nem titularidade — devolve `validity` sem aplicá-la, e o
código de uso sozinho libera a consulta. `interpret_fesn` faz as duas checagens por nós:
recusa carteirinha vencida e, quando a data de nascimento é informada, confere contra a
que veio no payload.

Ainda assim a cobertura é parcial: DCEs, CAs e outras entidades estaduais emitem CIE
legítima e nem todas têm API. **`não validou` significa `pendência documental`, nunca
`recusado`** — é a regra de produto que a POC codifica na mensagem de erro.

Para PcD e professor não existe rota consultável por empresa privada; ID Jovem só valida
manualmente. O levantamento completo está em [../docs/meia-entrada.md](../docs/meia-entrada.md).

## Testes

```bash
.venv/bin/pytest tests/test_cie.py -v
```

20 testes cobrem normalização de data, contrato do payload, interpretação de cada código
de retorno e os modos de falha do gateway — sem tocar a rede.
