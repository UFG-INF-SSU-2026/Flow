# Software para Sistemas Ubíquos — Atividade em Grupo 01

## Análise inicial de um sistema ubíquo

**Cenário escolhido:** Monitoramento e assistência a uma pessoa idosa

---

## Parte 1 - Compreensão do problema

### 1. Problema e usuários

Idosos que vivem sozinhos ou com supervisão apenas parcial estão sujeitos a riscos que muitas vezes passam despercebidos no dia a dia: quedas, longos períodos de imobilidade, alterações na rotina e exposição à luz em horários inadequados, o que pode prejudicar o sono e o bem-estar geral. O sistema pretende **perceber discretamente sinais do ambiente e do comportamento do idoso**, apoiando tanto a autonomia da pessoa quanto a tranquilidade de quem cuida dela.

- **Usuário primário:** a pessoa idosa monitorada (uso passivo, sem necessidade de interação direta).
- **Usuário secundário:** cuidadores e/ou familiares, que recebem informações e, futuramente, alertas.
- **Situação de uso:** ambiente residencial, com um smartphone Android atuando como coletor de dados, permanecendo fixo ou próximo ao idoso durante o dia.

---

## Estado atual do protótipo

Já foi criado, instalado e testado em um aparelho Android um APK que extrai dados de sensores que **não exigem permissão especial** e os envia para um servidor na mesma rede local, em **lotes periódicos de 10 segundos** (intervalo editável). Os sensores dos quais já foram obtidos dados são:

| Sensor | O que faz (resumo) |
| --- | --- |
| **TCS3701 Light** | Mede a intensidade de luz ambiente (luminosidade). |
| **TCS3701 Light CCT** | Mede a temperatura de cor da luz ambiente (luz "fria" ou "quente"). |
| **LIS2DLC12 Accelerometer** | Mede aceleração do dispositivo nos três eixos; indica movimento. |
| **Device Orientation Wake Up** | Detecta mudanças de orientação que "acordam" o sensor; indica manuseio do aparelho. |
| **Samsung GeoMagnetic Rotation Vector Sensor** | Combina acelerômetro e magnetômetro para calcular orientação absoluta do dispositivo. |
| **Samsung Orientation Sensor** | Fornece a orientação do dispositivo (ângulos de rotação). |
| **MXG4300S Magnetometer** | Mede o campo magnético calibrado; apoia o cálculo de orientação. |
| **MXG4300S Magnetometer Uncalibrated** | Mede o campo magnético sem correção de calibração; mesmo uso de apoio à orientação. |

---

### 2. Contexto

Já é possível perceber, com o protótipo atual, três dimensões de contexto:

- **Ambiente:** luminosidade e temperatura de cor da luz (CCT) do cômodo.
- **Usuário (indireto):** padrões de movimento e orientação, que servem como indício de atividade, imobilidade ou possíveis quedas.
- **Sistema:** orientação e posição do próprio dispositivo, usada como referência para interpretar os dados dos demais sensores.

Ainda faltam dimensões importantes de contexto (localização dentro da casa, sinais vitais, som ambiente), discutidas mais abaixo, na seção "Fase de crescimento do projeto".

### 3. Dispositivos e comunicação

- **Smartphone Android:** concentra os sensores internos e realiza a coleta contínua dos dados.
- **Servidor local:** recebe os dados enviados pelo aplicativo, na mesma rede Wi-Fi.
- **Comunicação:** dados enviados em **lotes periódicos a cada 10 segundos** (intervalo editável), dentro da mesma sub-rede, sem exposição à internet nesta fase — o que reduz superfície de ataque, mas ainda não resolve questões de persistência, escalabilidade e acesso remoto por cuidadores.

### 4. Processamento e resposta

A ideia central é que um **servidor central use os dados dos sensores para gerar algum tipo de informação ou ação**. Atualmente o processamento é centralizado no servidor local, que recebe os dados brutos, e já é possível esboçar respostas de valor a partir deles:

- **Recomendação sobre iluminação noturna**, a partir do sensor **TCS3701 Light / CCT**: ao medir a temperatura de cor da luz do quarto no início da noite, o sistema pode identificar se o ambiente está com luz "fria" (alta CCT, rica em luz azul), o que **inibe a produção de melatonina** e prejudica o sono do idoso.
- **Indício de atividade/queda**, a partir do acelerômetro **LIS2DLC12** e dos sensores de orientação (**Device Orientation Wake Up**, **Orientation Sensor**, **Rotation Vector**): variações bruscas seguidas de imobilidade prolongada e mudança de orientação (por exemplo, o dispositivo passando de vertical para horizontal de forma abrupta) são indícios indiretos de queda ou de longos períodos parado no mesmo lugar.
- **Contexto de uso do dispositivo**, a partir do **Device Orientation Wake Up**: indica quando o aparelho é manuseado, o que pode servir como proxy de interação/atividade do idoso com o ambiente.

A única ação (atuação) já prevista pelo grupo é: **avisar o cuidador caso alguma informação obtida pelo sistema mereça ser informada** (por exemplo, um possível indício de queda ou uma condição de luz inadequada à noite). Outras ações poderão ser adicionadas futuramente, mas essa é a única definida até o momento.

> **Conclusão de viabilidade:** mesmo restrito a sensores sem permissão especial, o protótipo já demonstra viabilidade técnica — os dados coletados (especialmente luz/CCT e movimento) sustentam recomendações e indícios relevantes para o cuidado do idoso, validando a ideia antes de investir em sensores mais sensíveis.

### 5. Risco principal

**Confiabilidade e completude dos dados**, com efeito direto sobre **privacidade**: como os sensores disponíveis nesta fase são apenas os que não exigem permissão especial, o sistema ainda **não capta diretamente sinais vitais ou localização**, o que limita a confiabilidade de qualquer inferência sobre queda ou emergência de saúde (falsos positivos/negativos). Ao mesmo tempo, dados de movimento e uso do dispositivo, mesmo indiretos, revelam rotina e hábitos do idoso — o que exige cuidado com armazenamento e acesso a esses dados, mesmo estando restritos à rede local por enquanto.

_Observação: o grupo ainda não definiu qual risco será priorizado na apresentação (confiabilidade, privacidade ou energia/bateria) — a decisão fica em aberto para discussão futura._

---

## Elementos já identificáveis (prévia da Parte 2)

### Sensores, atuadores e gateway

| Elemento | Itens identificados | Papel no sistema |
| --- | --- | --- |
| **Sensores** | TCS3701 Light / CCT, LIS2DLC12 Accelerometer, Device Orientation Wake Up, Orientation Sensor, GeoMagnetic Rotation Vector, Magnetometer (calibrado e não calibrado) | Percebem luminosidade, cor da luz, movimento e orientação, formando a base de contexto do sistema |
| **Atuadores** | Aviso ao cuidador (única ação definida até o momento) | Notificar o cuidador quando o servidor identificar uma informação relevante nos dados coletados |
| **Gateway** | O próprio smartphone Android | Agrega os dados dos sensores internos e os encaminha ao servidor local via Wi-Fi, em lotes a cada 10s, funcionando como ponto único de coleta e repasse |

### Classificação

O sistema pode ser caracterizado, nesta fase, como:

- **IoT (Internet das Coisas):** o smartphone atua como dispositivo conectado que envia dados continuamente a um servidor via rede.
- **Aplicação ubíqua:** o sensoriamento é contínuo e não exige interação explícita do idoso, integrando-se de forma discreta à rotina.

Ainda **não** se caracteriza plenamente como **sistema ciber-físico**, pois falta a etapa de atuação sobre o mundo físico (loop de controle fechado) — isso deve mudar quando o aviso ao cuidador (ou outros atuadores) for de fato implementado.

### Contexto e adaptação

- **Mudança de contexto:** transição dia/noite (variação de luminosidade e CCT) e transição atividade/imobilidade prolongada.
- **Adaptação esperada:** ao anoitecer, o sistema poderá priorizar o monitoramento de CCT e sugerir ajuste de iluminação; ao detectar imobilidade fora do padrão (ex.: parado por muito tempo em horário normalmente ativo), poderá aumentar a frequência de amostragem do acelerômetro para confirmar se é um possível caso de queda antes de gerar qualquer alerta.

---

## Fase de crescimento do projeto

**Importante: tudo o que está listado nesta seção são apenas ideias.** Nada aqui foi implementado, nem teve sua viabilidade averiguada — são apenas cenários que o grupo conseguiu observar como possíveis caminhos futuros. O fato de terem sido pensados **não significa que serão implementados**.

Os sensores usados até agora foram escolhidos por não exigirem permissão especial no Android, o que permitiu validar rapidamente a viabilidade técnica do projeto. Para cobrir cenários mais críticos de assistência ao idoso, os seguintes caminhos estão sendo pensados (sem compromisso de implementação):

- **GPS/localização**, para identificar em qual cômodo/área o idoso está;
- **Microfone**, para detecção de sons de queda ou pedidos de ajuda;
- **Contador de passos/giroscópio dedicado**, para refinar a detecção de atividade.

Além disso, está sendo pensada a possibilidade de incorporar dados de uma **pulseira que o idoso usaria** (ex.: sensores de frequência cardíaca, oximetria, etc.). Como o grupo **não viabiliza, em hipótese alguma, a compra de sensores/dispositivos** para a realização do trabalho, todos os dados vindos dessa pulseira seriam **mockados (gerados aleatoriamente) dentro de um limite pré-estabelecido**, apenas para simular esse cenário e permitir explorar a ideia sem depender de hardware real.
