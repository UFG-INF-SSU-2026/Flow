# Entregas da disciplina

Onde está cada item exigido no Marco 4 e na entrega final, o registro de atendimento ao feedback, as declarações, as
contribuições individuais e o uso de IA.

> Os campos marcados com *(a preencher)* são completados pelo grupo antes de cada entrega.

## Marco 4

| Item | Onde |
| --- | --- |
| Aplicativo móvel (APK) | [Releases](https://github.com/UFG-INF-SSU-2026/Flow/releases/latest); código em `SensorApp/`; usa acelerômetro, luz e localização do celular |
| Persistência | PostgreSQL com TimescaleDB ([`banco.md`](banco.md)); verificação V3 em [`testes.md`](testes.md) |
| Testes automatizados | [`testes.md`](testes.md): comandos, módulos cobertos, resultados e relação com os requisitos |
| Documentação de implantação | [`INSTRUCOES.md`](../INSTRUCOES.md) e [`operacao.md`](operacao.md) |
| Relatório técnico (artigo) | *(a preencher: caminho do PDF)* |
| Diagrama e distribuição | [`arquitetura.md`](arquitetura.md), seções 2 e 3 |
| Contratos e validação | [`schemas.md`](schemas.md) e `SensorServer/schemas/` |
| Regras com estado e tempo | [`arquitetura.md`](arquitetura.md), seções 8 e 9 |
| Autenticação, autorização e canal protegido | [`arquitetura.md`](arquitetura.md), seções 4, 5 e 13 |
| Falha e recuperação | [`arquitetura.md`](arquitetura.md), seção 12; verificações V7 a V9 em [`testes.md`](testes.md) |
| Vídeo (até 15 min) | *(a preencher: link acessível ao professor)* |

## Declarações

### Simulações

- O **ESP32** é simulado no Wokwi (placa, acelerômetro MPU6050, buzzer, LEDs e botões) e chega à rede pelo gateway do
  Wokwi (`wokwigw`). Não há hardware físico.
- A queda no Wokwi é provocada pelos controles do acelerômetro simulado. `SensorServer/simular_queda.py` reproduz uma
  queda sem celular nem Wokwi, falando com o servidor como o firmware.
- Os limiares de queda são valores iniciais, sem calibração com quedas reais.

### Dados pessoais

O sistema lida com dados de rotina e saúde da pessoa idosa (movimento, imobilidade, luz do ambiente) e com a
**localização** do celular. Tratamento: [`arquitetura.md`](arquitetura.md), seção 13.3 (validação, quem lê, sem valor em
log) e retenção em [`banco.md`](banco.md) (leituras 14 dias, histórico 90 dias). Os dados de teste são de contas
fictícias.

### Limitações

| Tipo | Limitação |
| --- | --- |
| Segurança | Lista completa em [`arquitetura.md`](arquitetura.md), seção 13.2 |
| Conectividade | Servidor local: celular, bridge e servidor precisam estar na mesma rede, e o IP digitado no app muda quando a rede muda. Sem acesso pela internet. Uma falha de rede descarta a leitura daquele ciclo (sem fila no dispositivo) |
| Desempenho | Um servidor só, para uma residência. No ESP32 simulado, cada handshake TLS leva cerca de 3,2 s (medido); o envio é a cada 1 s |
| Energia | O app envia a cada 1 s e mantém um serviço em primeiro plano com localização, o que consome bateria. **Consumo não medido** |

## Registro de atendimento ao feedback

Formato pedido: apontamento → alteração → evidência → situação.

### Feedback do Marco 3

| Apontamento | Alteração | Evidência | Situação |
| --- | --- | --- | --- |
| O bridge fazia coisa demais (comunicação com o servidor e autenticação), numa relação 1:1 com o dispositivo | O bridge virou um repassador HTTPS **fino e sem estado**, que atende vários dispositivos ao mesmo tempo (1:n). Login, tokens, renovação, envelope AES e certificado de cliente passaram para o próprio firmware; o bridge só repassa seis rotas fixas e o certificado do dispositivo, sem conta, sessão nem cifra | `Bridge/api.py`, `Bridge/bridge.py`; `Bridge/tests/unit/test_sem_estado_e_fronteiras.py` (nenhum módulo de sessão, tradução ou cifra); `testes_integrados/`; [`arquitetura.md`](arquitetura.md), 11.3 | Atendido |
| Alguns componentes ainda usavam HTTP em vez de HTTPS | TLS em todas as pontas, com CA própria: dispositivos ↔ borda, firmware ↔ bridge, bridge ↔ borda, borda ↔ análise e borda ↔ banco. Não existe listener HTTP; o firmware não compila com URL `http://` e o app proíbe texto claro. Os dispositivos também passaram a apresentar certificado de cliente. As duas exceções de rede (NTP e o gateway local do Wokwi) estão documentadas | `test_texto_claro_recusado.py` (servidor e bridge), `test_sem_texto_claro.py`, `test_tls.py`, `test_certificado_de_cliente.py`; [`arquitetura.md`](arquitetura.md), seção 5; verificação V6 em [`testes.md`](testes.md) | Atendido |
| Foi usado SSE em vez de WebSocket | Decisão mantida e justificativa reforçada: o retorno do dispositivo já vai por `POST` autenticado, então o canal de volta só precisa ser unidirecional. O SSE mantém o bridge fino (WebSocket o obrigaria a voltar a entender protocolo, o oposto do primeiro apontamento), reusa a pilha HTTPS com certificado do ESP32, é autorizado como qualquer rota e retoma o perdido com `Last-Event-ID`. A fraqueza (conexão que morre em silêncio) está declarada, e registrado quando reavaliar | [`arquitetura.md`](arquitetura.md), seção 10.5 (comparação com WebSocket e MQTT, custos aceitos); `test_push_sse.py`, `test_push_resiliencia.py`; medições V10 e V11 em [`testes.md`](testes.md) | Justificado (mantido) |
| *(a preencher: outros apontamentos, se houver)* | | | |

### Orientações gerais em aula

| Apontamento | Alteração | Evidência | Situação |
| --- | --- | --- | --- |
| Evitar potenciômetro para simular sensores | O Wokwi usa um acelerômetro MPU6050, que mede aceleração em três eixos como o do celular | `sketch/diagram.json`; [`wokwi.md`](wokwi.md) | Atendido |
| Mostrar atuadores com clareza | No celular: alarme, vibração, lanterna, voz e notificação com "Estou bem". No ESP32: buzzer, LEDs e botão. A resposta é gravada e o cuidador a vê | Verificação V1 em [`testes.md`](testes.md); `AtuadoresTest` | Atendido |
| Tratar sinal que deixa de chegar ou chega atrasado | Descarte de leitura atrasada ou repetida, resposta tardia classificada, episódio sem resposta encerrado como `EXPIRADO`, fila e reprocessamento com a análise fora, reconexão do push | `test_nucleo.py`, `test_regra_queda_retorno.py`, `test_push_resiliencia.py`; verificações V7 a V9 | Atendido |
| Persistência, de preferência temporal | PostgreSQL com TimescaleDB (leituras em hypertable) | `tests/banco/`; verificação V3 | Atendido |

## Contribuições individuais

| Integrante | Contribuição |
| --- | --- |
| *(a preencher)* | |

## Uso de IA

Registro pedido pela disciplina: ferramenta, finalidade, síntese das orientações, sugestões aproveitadas ou modificadas
e forma de validação.

| Ferramenta | Finalidade | Síntese das orientações | Aproveitado ou modificado | Validação |
| --- | --- | --- | --- | --- |
| *(a preencher)* | | | | |
