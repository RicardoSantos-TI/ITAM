# Inventário de TI — Agente + Servidor + Dashboard

MVP de gestão de ativos (ITAM): um agente coleta hardware, software, rede,
dispositivos conectados e saúde de cada máquina e envia para um servidor
central com dashboard.

```
inventario-ti/
├── agent/          # roda em cada máquina
│   ├── agent.py
│   ├── config.json
│   └── requirements.txt
└── server/         # roda num servidor central (1 instância)
    ├── server.py
    ├── dashboard.html
    └── requirements.txt
```

---

## 1. Subir o servidor (faça primeiro)

```bash
cd server
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# defina a chave que o agente vai usar (use uma forte)
set API_KEY=uma-chave-bem-grande-aqui              # Linux/mac: export API_KEY=...
uvicorn server:app --host 0.0.0.0 --port 8000
```

Abra `http://IP-DO-SERVIDOR:8000` — o dashboard aparece vazio até o primeiro envio.

## 2. Testar o agente

```bash
cd agent
pip install -r requirements.txt

# só coletar e ver o JSON, sem enviar (ótimo para validar a coleta):
python agent.py --dry-run

# editar config.json: server_url -> http://IP-DO-SERVIDOR:8000/api/ingest
#                     api_key    -> a MESMA chave do servidor
python agent.py            # envia uma vez
python agent.py --loop     # fica enviando no intervalo do config
```

Atualize o dashboard e a máquina aparece.

---

## 3. Empacotar o agente em `.exe`

No **Windows**, com o venv do agente ativo:

```bash
pip install pyinstaller
pyinstaller --onefile --name AgenteInventario ^
  --add-data "config.json;." agent.py
```

Sai em `dist/AgenteInventario.exe`. Ele lê o `config.json` ao lado dele.

## 4. Rodar como serviço (coleta automática em background)

Opção simples e robusta — Agendador de Tarefas do Windows:

```bat
schtasks /create /tn "AgenteInventario" /tr "C:\Agente\AgenteInventario.exe --loop" ^
  /sc onstart /ru SYSTEM /rl HIGHEST
```

Rodar como `SYSTEM` garante acesso a WMI/registro. Para serviço "de verdade"
(com recuperação automática), use [NSSM](https://nssm.cc) apontando para o exe.

---

## 5. ⚠️ Convivência com o Kaspersky (leia)

Um agente que coleta dados do sistema e os envia pela rede tem, por natureza,
um comportamento que **heurísticas de antivírus podem sinalizar** — ainda mais
quando empacotado com PyInstaller, que é famoso por gerar falsos-positivos.
Isso não é ilegalidade nenhuma; é só o AV fazendo o trabalho dele. Como resolver
do jeito certo, sem desativar o Kaspersky (você disse que não pode, e não deve):

1. **Assine o executável digitalmente** (code signing). É o fator que mais reduz
   falso-positivo. Um certificado de assinatura de código faz o binário ser
   reconhecido como legítimo e da sua organização.
2. **Adicione à exclusão pela console central**, não máquina a máquina. Se a
   empresa usa **Kaspersky Security Center**, o admin cria uma exceção confiável
   para o hash/caminho do agente e distribui por política. Esse é o caminho
   corporativo correto e some com o problema de vez.
3. **Não ofusque o código.** Ofuscação é um sinal clássico de malware. Mantenha
   o binário limpo, com metadados de versão/empresa preenchidos.
4. **Durante o desenvolvimento**, use as máquinas fora do domínio (como você já
   planejou). Só leve para o parque com Kaspersky depois de assinado e com a
   exclusão publicada na console.

Enquanto não tiver certificado, para os testes você pode empacotar sem
PyInstaller (rodar via `python agent.py` direto), que costuma disparar bem menos.

---

## 6. Boas práticas de conformidade (para a apresentação à gestora)

Como isso mexe com dados de máquinas e usuários, vale já chegar com o dever de
casa feito — fortalece a proposta:

- **Transparência**: informe os colaboradores que os ativos têm um agente de
  inventário e o que ele coleta. Transparência é o que separa ITAM legítimo de
  vigilância.
- **Minimização**: o agente coleta inventário e saúde, não conteúdo de arquivos,
  navegação nem digitação. Mantenha assim.
- **Segurança do canal**: em produção troque `http` por **HTTPS** (coloque o
  servidor atrás de um proxy com TLS, ex. Caddy/Nginx) e mantenha `verify_tls: true`.
- **Autenticação**: a `API_KEY` é o mínimo. Para produção, considere uma chave
  por máquina e rotação.
- **LGPD**: registre a finalidade (gestão de ativos e suporte) e a base legal
  com o responsável/DPO da empresa.

---

## 7. Próximo passo: suporte técnico remoto

Você citou usar o agente para prestar suporte. Isso é uma segunda fase (território
RMM) e muda o perfil de risco — envolve **ação** na máquina, não só leitura.
Sugestão de evolução, em ordem de esforço:

1. **Comandos de diagnóstico** de catálogo fechado (ex.: "limpar temporários",
   "reiniciar spooler"), acionados pelo dashboard e executados pelo agente a
   partir de uma lista pré-aprovada — nunca comando arbitrário remoto.
2. **Coleta sob demanda** (forçar um inventário fora do intervalo).
3. **Acesso remoto à tela** — aqui o mais seguro é integrar uma ferramenta
   madura e auditável (ex.: túnel para RustDesk/AnyDesk) em vez de escrever do
   zero, com consentimento explícito na tela do usuário a cada sessão.

Posso desenvolver a fase 1 (comandos de catálogo) quando o inventário estiver
validado no seu ambiente.
```
