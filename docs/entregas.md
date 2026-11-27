# Entregas da disciplina

Índice dos itens exigidos no Marco 4 e na entrega final, e o que não está no relatório técnico: o registro de
atendimento ao feedback e a declaração sobre dados pessoais.

O **relatório técnico** ([`relatorio.pdf`](../relatorio.pdf)) traz os
resultados dos testes e das verificações, com as evidências, as limitações, as contribuições individuais e a declaração
de uso de IA. Esses itens não se repetem aqui.

## Marco 4

| Item | Onde |
| --- | --- |
| Aplicativo móvel (APK) | [Releases](https://github.com/UFG-INF-SSU-2026/Flow/releases/latest); código em `SensorApp/`; usa acelerômetro, luz e localização do celular |
| Persistência | PostgreSQL com TimescaleDB ([`banco.md`](banco.md)); roteiro V3 em [`testes.md`](testes.md), resultado no relatório |
| Testes automatizados | Comandos, módulos cobertos e relação com os requisitos em [`testes.md`](testes.md); resultados e evidências no relatório |
| Documentação de implantação | [`INSTRUCOES.md`](../INSTRUCOES.md) e [`operacao.md`](operacao.md) |
| Relatório técnico (artigo) | [`relatorio.pdf`](../relatorio.pdf) |
| Diagrama e distribuição | [`arquitetura.md`](arquitetura.md), seções 2 e 3 |
| Contratos e validação | [`schemas.md`](schemas.md) e `SensorServer/schemas/` |
| Regras com estado e tempo | [`arquitetura.md`](arquitetura.md), seções 8 e 9 |
| Autenticação, autorização e canal protegido | [`arquitetura.md`](arquitetura.md), seções 4, 5 e 13 |
| Falha e recuperação | [`arquitetura.md`](arquitetura.md), seção 12; roteiro V7 a V9 em [`testes.md`](testes.md), resultado no relatório |
| Vídeo (até 15 min) | https://www.youtube.com/watch?v=hzzMxxI1THw |
| Limitações, contribuições individuais e uso de IA | Relatório técnico |

## Declarações

### Simulações e limitações

No relatório técnico ([`relatorio.pdf`](../relatorio.pdf)). A lista
técnica completa está em [`arquitetura.md`](arquitetura.md), seção 13.2.

### Dados pessoais

O sistema lida com dados de rotina e saúde da pessoa idosa (movimento, imobilidade, luz do ambiente) e com a
**localização** do celular. Tratamento: [`arquitetura.md`](arquitetura.md), seção 13.3 (validação, quem lê, sem valor em
log) e retenção em [`banco.md`](banco.md) (leituras 14 dias, histórico 90 dias). Os dados de teste são de contas
fictícias.

## Registro de atendimento ao feedback

Formato pedido: apontamento → alteração → evidência → situação.

### Feedback do Marco 3

| Apontamento | Alteração | Evidência | Situação |
| --- | --- | --- | --- |
| O bridge fazia coisa demais (comunicação com o servidor e autenticação), numa relação 1:1 com o dispositivo | O bridge virou um repassador HTTPS **fino e sem estado**, que atende vários dispositivos ao mesmo tempo (1:n). Login, tokens, renovação, envelope AES e certificado de cliente passaram para o próprio firmware; o bridge só repassa seis rotas fixas e o certificado do dispositivo, sem conta, sessão nem cifra | `Bridge/api.py`, `Bridge/bridge.py`; `Bridge/tests/unit/test_sem_estado_e_fronteiras.py` (nenhum módulo de sessão, tradução ou cifra); `testes_integrados/`; [`arquitetura.md`](arquitetura.md), 11.3 | Atendido |
| Alguns componentes ainda usavam HTTP em vez de HTTPS | TLS em todas as pontas, com CA própria: dispositivos ↔ borda, firmware ↔ bridge, bridge ↔ borda, borda ↔ análise e borda ↔ banco. Não existe listener HTTP; o firmware não compila com URL `http://` e o app proíbe texto claro. Os dispositivos também passaram a apresentar certificado de cliente. As duas exceções de rede (NTP e o gateway local do Wokwi) estão documentadas | `test_texto_claro_recusado.py` (servidor e bridge), `test_sem_texto_claro.py`, `test_tls.py`, `test_certificado_de_cliente.py`; [`arquitetura.md`](arquitetura.md), seção 5; verificação V6 em [`testes.md`](testes.md) | Atendido |
| Foi usado SSE em vez de WebSocket | Decisão mantida e justificativa reforçada: o retorno do dispositivo já vai por `POST` autenticado, então o canal de volta só precisa ser unidirecional. O SSE mantém o bridge fino (WebSocket o obrigaria a voltar a entender protocolo, o oposto do primeiro apontamento), reusa a pilha HTTPS com certificado do ESP32, é autorizado como qualquer rota e retoma o perdido com `Last-Event-ID`. A fraqueza (conexão que morre em silêncio) está declarada, e registrado quando reavaliar | [`arquitetura.md`](arquitetura.md), seção 10.5 (comparação com WebSocket e MQTT, custos aceitos); `test_push_sse.py`, `test_push_resiliencia.py`; medições V10 e V11 em [`testes.md`](testes.md) | Justificado (mantido) |

### Orientações gerais em aula

| Apontamento | Alteração | Evidência | Situação |
| --- | --- | --- | --- |
| Evitar potenciômetro para simular sensores | O Wokwi usa um acelerômetro MPU6050, que mede aceleração em três eixos como o do celular | `sketch/diagram.json`; [`wokwi.md`](wokwi.md) | Atendido |
| Mostrar atuadores com clareza | No celular: alarme, vibração, lanterna, voz e notificação com "Estou bem". No ESP32: buzzer, LEDs e botão. A resposta é gravada e o cuidador a vê | Verificação V1 em [`testes.md`](testes.md); `AtuadoresTest` | Atendido |
| Tratar sinal que deixa de chegar ou chega atrasado | Descarte de leitura atrasada ou repetida, resposta tardia classificada, episódio sem resposta encerrado como `EXPIRADO`, fila e reprocessamento com a análise fora, reconexão do push | `test_nucleo.py`, `test_regra_queda_retorno.py`, `test_push_resiliencia.py`; verificações V7 a V9 | Atendido |
| Persistência, de preferência temporal | PostgreSQL com TimescaleDB (leituras em hypertable) | `tests/banco/`; verificação V3 | Atendido |

## Contribuições individuais

No relatório técnico ([`relatorio.pdf`](../relatorio.pdf)).

## Uso de IA

No relatório técnico ([`relatorio.pdf`](../relatorio.pdf)).
