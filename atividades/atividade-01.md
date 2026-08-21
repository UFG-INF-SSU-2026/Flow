# Atividade em Grupo 01 - Análise inicial de um sistema ubíquo

**Cenário escolhido:** Monitoramento e assistência a uma pessoa idosa

---

## Parte 1 - Compreensão do problema

**1. Problema e usuários**
Muitos idosos moram sozinhos ou passam boa parte do dia sem supervisão direta, o que aumenta o risco de quedas ou episódios de imobilidade sem que ninguém perceba a tempo. A demora na identificação de uma emergência agrava o risco à saúde. O sistema busca detectar sinais de atividade anormal e alertar um cuidador em tempo real, sem depender de o próprio idoso pedir ajuda.
- **Usuário primário (monitorado):** o idoso, que usa um dispositivo vestível (wearable) com sensor de movimento e carrega um celular com aplicativo próprio.
- **Usuário secundário (cuidador):** familiar ou cuidador que recebe os alertas e acompanha o status pelo aplicativo.

**2. Contexto**
- Sobre o usuário: aceleração/movimento captado pelo wearable, padrão de atividade habitual, horário do dia (esperado dormir à noite, se movimentar de dia), cômodo da casa em que está (se houver sensores de presença), estado de confirmação (se respondeu ou não ao "estou bem?"), e localização do idoso via GPS do celular (dentro ou fora de casa), útil para saber se a ausência de movimento se deve a uma saída, não a uma emergência.
- Sobre o ambiente: ausência de interação por período prolongado, o que pode indicar inatividade suspeita; presença de outra pessoa em casa (visita/cuidador presencial), que pode dispensar um alerta automático; temperatura do ambiente, relevante em casos de mal-estar por calor/frio extremo.
- Sobre o sistema: bateria do dispositivo (wearable e celular), qualidade da conexão de rede, horário da última telemetria recebida (heartbeat) de cada sensor, histórico de alertas recentes (para evitar notificações repetidas do mesmo evento).

Esse contexto ajuda a diferenciar uma situação normal (ex.: idoso dormindo, ou fora de casa) de uma potencialmente crítica (ex.: queda seguida de imobilidade fora do padrão, dentro de casa).

**3. Dispositivos e comunicação**
- Wearable (pulseira/relógio) com acelerômetro, que publica a telemetria de movimento periodicamente.
- Um gateway/hub doméstico recebe os dados via Bluetooth e os encaminha à internet.
- Aplicativo no celular do idoso, que cumpre duas funções: (a) interface para confirmar "estou bem", e (b) sensor complementar, usando o GPS do aparelho para detectar se o idoso está dentro ou fora de casa.
- Aplicativo no celular do cuidador (para receber alertas).

**4. Processamento e resposta**
Um serviço local (na borda, perto do gateway) aplica regras simples de detecção: pico de aceleração seguido de imobilidade prolongada, ou ausência total de movimento por um tempo configurável. A leitura de GPS do celular é usada como contexto auxiliar: se indicar que o idoso está fora de casa, a regra de imobilidade doméstica é suspensa, já que não é esperado movimento do wearable dentro de um ambiente que ele não está ocupando. Ao suspeitar de um evento, o sistema primeiro pede confirmação ao idoso (botão "estou bem" no app, com um tempo limite). Se não houver resposta, o alerta é confirmado e uma notificação é enviada ao cuidador (push, Telegram ou e-mail), com horário, tipo de evento e localização aproximada (se disponível via GPS).

**5. Risco principal**
O risco mais crítico é o equilíbrio entre **falso-positivo/falso-negativo** e **privacidade**. Alertas em excesso (ex.: tirar o relógio) geram fadiga de alarme no cuidador, enquanto quedas suaves podem não ultrapassar o limiar configurado e passar despercebidas. Ao mesmo tempo, os dados de movimento **e de localização** são sensíveis — o GPS, em particular, expõe não só se o idoso saiu de casa, mas também para onde foi — então é importante o idoso poder pausar o monitoramento (movimento e localização) em momentos de privacidade (banho, visitas, saídas pessoais, por exemplo) — um equilíbrio entre proteção e autonomia que deve ser decidido junto com os usuários.

---

## Parte 2 - Modelagem do sistema

**6. Sensores, atuadores e gateway**
- **Sensores:**
  - Acelerômetro do wearable, que detecta picos de movimento (possível queda) e imobilidade prolongada.
  - GPS do celular do idoso, usado como sensor complementar para identificar se ele está dentro ou fora de casa, ajudando a contextualizar períodos sem telemetria do wearable.
- **Atuadores:** botão/tela do app do idoso (para confirmar "estou bem"), notificação push no celular do cuidador, alarme sonoro do wearable.
- **Gateway:** hub doméstico que recebe a telemetria do wearable via Bluetooth e publica os dados na rede (ex.: via protocolo MQTT) para o serviço de processamento. O celular do idoso publica a leitura de GPS diretamente à internet (via rede móvel ou Wi-Fi), sem depender do hub.

**7. Fluxo do sistema**
```
Queda ou imobilidade prolongada (fenômeno físico)
        ↓
Acelerômetro do wearable (sensor)              GPS do celular do idoso (sensor complementar)
        ↓                                                     ↓
Hub doméstico publica via MQTT (gateway)        Celular publica localização via internet
        ↓                                                     ↓
Serviço local aplica regras de detecção (processamento), cruzando movimento do wearable com localização (dentro/fora de casa)
        ↓
Aguarda confirmação do idoso; sem resposta = alerta confirmado (decisão)
        ↓
Notificação ao cuidador, com localização aproximada + registro no dashboard (resposta/atuador)
```

**8. Classificação**
O sistema pode ser classificado como:
- **IoT**, pois conecta o wearable, o hub e o celular (via GPS) à internet para troca de dados.
- **Sistema ciber-físico**, pois interage com o mundo físico (detecta quedas e localização) e atua sobre ele (dispara alertas).
- **Aplicação ubíqua**, pois monitora o idoso de forma contínua e discreta, combinando múltiplos sensores (movimento e localização) sem exigir interação constante — só pedindo confirmação quando há suspeita de evento.

**9. Contexto e adaptação**
Durante a noite, o sistema pode ajustar o limiar de "imobilidade suspeita", já que é esperado que o idoso fique parado dormindo. Se a bateria do wearable ou do celular estiver baixa, ou o heartbeat (última telemetria) de qualquer um dos dois não chegar no tempo esperado, o sistema deve tratar isso como um sinal de atenção adicional, mesmo sem confirmar uma queda. Se o GPS indicar que o idoso saiu de casa, o sistema suspende temporariamente os alertas de imobilidade doméstica, já que a ausência de movimento captada pelo wearable é esperada nesse caso — e passa a monitorar apenas por eventos de emergência reportados manualmente. Também deve existir um modo "pausa" que o idoso pode ativar em momentos de privacidade (banho, visitas, saídas pessoais, por exemplo), suspendendo temporariamente tanto os alertas de movimento quanto o compartilhamento de localização.

