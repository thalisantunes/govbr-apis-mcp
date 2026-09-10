# Meia-entrada: o que dá para validar por API

Levantamento de 2026-08-07 para uso em ticketing (setor privado), sob a Lei 12.933/2013
e o Decreto 8.537/2015. Cada afirmação abaixo foi verificada contra o endpoint ou a
página oficial; o que não deu para confirmar está marcado como tal.

## Resumo por beneficiário

| Beneficiário | Rota para empresa privada | Como |
|---|---|---|
| Estudante (CIE) | **Sim, API REST** | `meiaentrada-cie-validador` — base DNE (UNE/UBES/ANPG) |
| ID Jovem | **Sim, mas manual** | site/app com cadastro por CNPJ; API só via pleito à SNJ |
| PcD | **Não existe** | documental (Certificado Meu INSS, laudo, carteiras estaduais) |
| Professor | **Não existe** | documental (carteira funcional, holerite) |

## Estudante — CIE

`POST https://api-prod.meiaentrada.org.br/public/validador`, header `codigoAcesso`,
body `{codigoUso, dataNascimento (YYYYMMDD), nomeEvento, dataEvento (YYYYMMDD), cnpjPromotor}`.
`codigoUso` e `dataNascimento` são os dois parâmetros da URL do QR code da carteirinha;
`certificado` é campo de resposta, não de request. Devolve status, nome, nome social, CPF,
foto, instituição, curso, nível e expiração — o suficiente para conferir titularidade na
portaria. O cadastro de estabelecimento/produtora é aberto ao setor privado em
<https://www.meiaentrada.org.br/validador/integre>; na prática o header `codigoAcesso` não
é verificado hoje, mas o cadastro remove o risco de bloqueio futuro.

Segunda emissora confirmada: **FESN** (`GET https://validade.fesn.org.br/api/students/usecode/{codigo}`,
sem auth e sem data de nascimento), descoberta quando uma carteirinha real do IFRS falhou
no DNE. Contrato completamente diferente do DNE — cada emissora terá o seu.

Limite real: cobre a base DNE. A lei também permite emissão por DCEs, CAs e entidades
estaduais, que mantêm validadores próprios sem API documentada. Um estudante com
carteira legítima fora do DNE **falha** nessa consulta — o fluxo precisa de fallback
documental, não de recusa automática.

## ID Jovem

Validação por instituição privada existe e é oficial (página gov.br "Validar a ID Jovem"),
mas por vias manuais: validador web (`idjovem.juventude.gov.br/validarcarteira`, SPA atrás
de WAF — endpoint de backend não confirmado como API) e o app ID Jovem no fluxo
"Sou empresário", com cadastro por CNPJ.

Para integração sistêmica, o caminho é pleitear a API `conecta-id-jovem` à Secretaria
Nacional da Juventude. Detalhe que vale registrar: das 95 APIs do Conecta, essa é a única
cuja descrição oficial admite instituições **públicas e privadas** — a regra geral de
"só órgão público" tem essa exceção.

## PcD — não há rota

Sendo direto: não existe registro nacional de PcD consultável por empresa privada.

- RRPD (`conecta-pessoa-com-deficiencia`) e BPC (`conecta-beneficio-de-prestacao-continuada-bpc`)
  resolveriam por CPF, mas são restritos a órgãos federais.
- O que o cidadão consegue emitir sozinho é o Certificado de Pessoa com Deficiência do
  Meu INSS (PDF com QR, validade 90 dias) — conferível na portaria pelo QR, sem API nem
  consulta em lote, e só para quem está nas bases BPC ou LC 142.
- Carteiras estaduais (CIPTEA e afins) são fragmentadas por UF, sem consulta unificada.

Abrir o RRPD para privados exigiria mudança normativa (Resolução CCGD 10/2022), não adesão.

## Professor — não há rota

Meia-entrada de professor vem de leis estaduais/municipais (RJ 8.775/2020, SP 14.729/12 e
15.298/14, entre outras) e a comprovação é 100% documental: carteira funcional da Secretaria
de Educação, holerite ou declaração da escola. Não há registro digital consultável, nem
estadual. Ingresso.com, Cinemark, Ticketmaster, Eventim e Sympla fazem revisão manual.

## e-MEC / INEP — validação indireta

Não há API REST oficial do e-MEC (o site responde 403 a cliente não-browser). O que existe
é o dataset "Sistema e-MEC — IES do Brasil" no dados.gov.br e os microdados do INEP, ambos
institucionais: servem para conferir se a instituição declarada na CIE existe e está ativa,
nunca para validar o vínculo de um aluno.

## Consequência de desenho

Nenhuma dessas fontes valida sem fricção no momento da compra online. O desenho realista é
híbrido: validação automática onde existe código (CIE via API, ID Jovem via código/QR) e
pré-aprovação documental assíncrona para PcD e professor. Qualquer promessa de "validação
automática de meia-entrada" acima disso não se sustenta nas fontes disponíveis hoje.
