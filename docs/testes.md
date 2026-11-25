# Testes e verificações

Testes automatizados de cada componente, o que cobrem, os resultados e o roteiro das verificações manuais.

## Como rodar

| Suíte | Comando | Precisa de |
| --- | --- | --- |
| Servidor | `cd SensorServer && python -m pytest` | `pip install -r requirements.txt` |
| Servidor no banco real | `cd SensorServer && python -m pytest -m banco` | Banco no ar (`scripts/preparar.sh`); usa um banco descartável `flow_teste` |
| Bridge | `cd Bridge && python -m pytest` | `pip install -r requirements.txt` |
| Ponta a ponta | `cd testes_integrados && python -m pytest` | Dependências do servidor e do bridge |
| App (JVM) | `cd SensorApp && ./gradlew test` | JDK 21 e Android SDK ([`operacao.md`](operacao.md#compilar-o-app)) |
| Firmware no PC | ver `sketch/host_test/README.md` | `g++`, mbedTLS (`libmbedtls-dev`) e ArduinoJson 7 |

Nenhuma suíte lê o `.env` real nem usa o `certs/` do projeto: cada uma cria segredos e CAs de teste numa pasta
temporária. As suítes Python não usam rede externa.

## Resultados

Versão de 25/11/2026, em Linux com Python 3.12.

| Suíte | Resultado |
| --- | --- |
| Servidor | 1194 passaram |
| Servidor no banco real (`-m banco`) | passou (execução do grupo) |
| Bridge | 118 passaram |
| Ponta a ponta | 11 passaram |
| App (JVM) | passou (execução do grupo) |
| Firmware no PC | 128 verificações `PASS`, `FIRMWARE OK 0`; `verificar_contra_python.py`: o CSR do firmware é aceito pela borda |

## O que cada suíte cobre

| Suíte | Módulos | Como |
| --- | --- | --- |
| Servidor, unitários (`SensorServer/tests/unit/`) | Autenticação e contas, autorização, emissão de certificado, regras (queda, imobilidade, luz) e núcleo da análise, mensagens de push, hub SSE, validação de contratos, TLS, geração de certificados, migrações, fronteiras entre pacotes | Funções isoladas, com relógio congelado onde há tempo |
| Servidor, integração (`SensorServer/tests/integration/`) | Rotas HTTP, autenticação e autorização por rota, certificado de cliente (app direto e Wokwi pelo bridge), rotas do cuidador, push SSE e reconexão, falha de canal, isolamento entre pessoas e dispositivos, limite de corpo, banco e análise fora do ar, queda ponta a ponta | Borda real com banco em memória e análise em memória, chamada pelo mesmo cliente da produção. Os testes de certificado de cliente, de push e de queda ponta a ponta rodam com **TLS real** em loopback; `test_texto_claro_recusado.py` sobe os processos reais e confere que só aceitam HTTPS |
| Servidor no banco real (`SensorServer/tests/banco/`) | O mesmo contrato do repositório, agora no PostgreSQL com TimescaleDB | Banco real descartável |
| Bridge (`Bridge/tests/`) | Lista fechada de rotas, repasse sem alterar corpo, certificado de cliente do dispositivo, repasse do certificado à borda (apagando o cabeçalho forjado), identidade própria do bridge, SSE em fluxo, falhas da borda (`502`/`504`), reinício, texto claro recusado | Bridge real em TLS contra uma borda falsa, com PKI de teste |
| Ponta a ponta (`testes_integrados/`) | Firmware simulado → bridge real → borda real | Login e emissão sem certificado; telemetria e push com certificado; `401 certificado_ausente`; `403 certificado_incompativel`; cabeçalho forjado apagado; Wokwi direto na borda recusado |
| App (`SensorApp/app/src/test/`) | Validação das mensagens de push, CSR, decisão de buscar a CA, política de sessão, classificação de falha de certificado, atuadores, bloco de localização, tela do cuidador, textos da tela | Lógica pura na JVM, sem emulador |
| Firmware (`sketch/host_test/`) | Envelope AES-GCM, login e tokens, geração de chave e CSR, ordem do boot, falha fechada sem certificado, reemissão, leitura do push, buzzer, LEDs e máquina de imobilidade | Arduino falso e servidor falso no PC; envelope e CSR conferidos contra o código do servidor |

**O que não tem teste automatizado:** a interface do app, os sensores e os avisos reais no aparelho; o transporte TLS
real do ESP32 (`transporte_wifi.h`) no Wokwi. Esses pontos são cobertos pelas verificações manuais abaixo.

## Requisitos e testes

| Requisito | Testes que o cobrem |
| --- | --- |
| Dois produtores, decisão e resposta | `test_queda_ponta_a_ponta.py`, `test_regra_queda_*.py`, `test_nucleo.py`, `testes_integrados/` |
| Mudança de contexto (dia/noite, movimento/repouso) | `test_regras_legadas.py`, `test_regra_queda_estado.py`, `test_nucleo.py` |
| Estado, tempo, validade e atrasos | `test_regra_queda_retorno.py` (resposta tardia), `test_nucleo.py` (deduplicação e dado atrasado), `test_push_resiliencia.py` (expiração) |
| Persistência | `test_repo_contrato.py` (banco em memória) e `tests/banco/` (PostgreSQL real), `test_banco_servidor.py` |
| Autenticação e identidade inválida | `test_auth.py`, `test_auth_http.py`, `test_certificado_de_cliente.py` |
| Autorização (operação fora da permissão) | `test_authz.py`, `test_authz_http.py` (matriz papel × rota), `test_cuidador.py`, `test_user_id_http.py`, `test_isolamento_dispositivos.py` |
| Canal protegido | `test_tls.py`, `test_texto_claro_recusado.py` (servidor e bridge), `test_sem_texto_claro.py`, `test_certificado_de_cliente.py` |
| Falha e recuperação | `test_banco_servidor.py`, `test_push_resiliencia.py`, `test_falha_de_canal.py`, `Bridge/tests/integration/test_falhas.py` |
| Contratos | `test_contratos.py`, `test_validacao.py`, `test_schema_version_independente.py` |

## Verificações manuais

Roteiro com o sistema completo no ar ([`INSTRUCOES.md`](../INSTRUCOES.md)). As colunas **Observado** e **Evidência**
são preenchidas na execução (resultado visto e print, vídeo ou trecho de log).

| # | Verificação | Passos e entradas | Esperado | Observado | Evidência |
| --- | --- | --- | --- | --- | --- |
| V1 | Fluxo e contexto | Celular (`idoso1`) e Wokwi (`bridge1`) ligados à mesma pessoa. Movimento brusco no celular e repouso | Alarme no celular **e** buzzer no Wokwi; o primeiro "Estou bem" encerra os dois; o cuidador vê o episódio | | |
| V2 | Mudança de contexto | Celular sob luz fria (CCT alto) antes e depois das 18 h (ou ajustando `HORA_INICIO_NOITE`) | De dia, nada; à noite, alerta de luz gravado e visível no histórico do cuidador | | |
| V3 | Persistência | Gerar leituras e um episódio; parar borda, análise e banco (`docker compose stop db`); subir de novo | Leituras, episódio, contas e sessões continuam no banco; o app segue logado | | |
| V4 | Identidade inválida | `curl` sem token em `/dados`; login com senha errada; chamada com token válido e sem certificado de cliente | `401`; `401` (e `429` depois de 5 tentativas); `401 certificado_ausente` | | |
| V5 | Fora da permissão | Cuidador do `user_id` 1 pedindo `/cuidador/2/historico`; conta `idoso` pedindo `GET /dados` | `403 fora_de_escopo`; `403` | | |
| V6 | Canal criptografado | `curl http://<IP>:5000/health` (texto claro); captura do tráfego com Wireshark | Conexão recusada; só TLS na captura | | |
| V7 | Falha e recuperação: análise | Parar a análise com o app enviando; esperar; subir de novo | `/health` mostra `degradado` e as leituras seguem gravadas; ao voltar, `ok` e a fila é processada | | |
| V8 | Falha e recuperação: rede do celular | Desligar o Wi-Fi do celular por 30 s e religar | O app mostra a falha, reconecta sozinho e volta a receber pedidos | | |
| V9 | Falha e recuperação: sem resposta | Abrir um episódio e não tocar em "Estou bem" | No fim do prazo o episódio fecha como `EXPIRADO`, gravado e visível no histórico do cuidador | | |
| V10 | Latência do push | Com o celular em **Iniciar**, provocar uma queda (ou `simular_queda.py`) e gravar em vídeo, juntos, o terminal da borda e o celular. Medir pelo vídeo o tempo entre a linha `[push] queda.confirmacao_solicitada ... canal=app` e o alarme tocar. Repetir 5 vezes | Alarme em menos de 1 s depois da decisão; registrar média e máximo | | |
| V11 | Reconexão do push | Com o celular em **Iniciar**, desligar o Wi-Fi do celular por 10 s e religar; logo depois, provocar uma queda | O app reconecta sozinho (estado "conectado") e o pedido chega; registrar o tempo até reconectar | | |
