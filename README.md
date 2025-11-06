# Sistema Clínico Vida+

Sistema web de agendamento e gestão clínica desenvolvido em Flask, com foco em fluxos coordenados entre pacientes, recepcionistas e médicos. O projeto reúne autenticação por perfis, reservas de horários com prevenção de conflitos, solicitações de ajuste e um painel completo para acompanhamento dos procedimentos.

> **Aviso de direitos:** todos os direitos de uso estão reservados pessoalmente ao criador deste software. Nenhuma redistribuição, modificação ou uso comercial é permitido sem autorização expressa.

## Sumário
- [Visão geral](#visão-geral)
- [Principais funcionalidades](#principais-funcionalidades)
  - [Experiência do paciente](#experiência-do-paciente)
  - [Experiência da recepção](#experiência-da-recepção)
  - [Experiência do médico](#experiência-do-médico)
- [Arquitetura e tecnologias](#arquitetura-e-tecnologias)
- [Estrutura de diretórios](#estrutura-de-diretórios)
- [Configuração e execução](#configuração-e-execução)
  - [Requisitos](#requisitos)
  - [Passo a passo](#passo-a-passo)
- [Dados iniciais e cadastros](#dados-iniciais-e-cadastros)
- [Fluxo de agendamento e ajustes](#fluxo-de-agendamento-e-ajustes)
- [Estilos e responsividade](#estilos-e-responsividade)
- [Testes rápidos](#testes-rápidos)
- [Dicas para evolução](#dicas-para-evolução)
- [Direitos autorais](#direitos-autorais)

## Visão geral
O sistema oferece uma visão centralizada da rotina da clínica "Vida+", permitindo registrar pacientes, organizar procedimentos, alocar salas e sincronizar agendas entre todos os papéis envolvidos. A camada visual adota uma identidade em tons de azul, com layouts responsivos que se adaptam tanto a desktop quanto a dispositivos móveis.

## Principais funcionalidades

### Experiência do paciente
- Cadastro e login dedicados.
- Visualização da agenda própria com status claros de cada consulta.
- Solicitação de ajustes (data e horário) apenas para horários realmente disponíveis, acompanhando o status do pedido em tempo real.
- Acesso a resumos de procedimentos, profissionais e sala envolvidos.

### Experiência da recepção
- Painel principal com consultas do dia, filtros por status e indicadores rápidos.
- Tela especializada de **Procedimentos** para criar, editar e remover tipos de atendimento.
- Controle sobre mudanças de horário, sala, profissional e status de cada agendamento.
- Marcação de procedimentos como agendados, em atendimento, concluídos ou cancelados com feedback imediato.
- Offcanvas e modais organizam os formulários, evitando que o usuário perca o contexto da lista de agendamentos.

### Experiência do médico
- Painel com consultas futuras e pendências, destacando pacientes, procedimentos e horários.
- Possibilidade de acompanhar solicitações de ajuste aprovadas pela recepção.

## Arquitetura e tecnologias
- **Backend:** Python 3 + Flask, organizado em blueprints (`routes/user.py`).
- **Banco de dados:** SQLite com criação automática de tabelas e sementes idempotentes (`databaser.py`).
- **Autenticação:** sessão server-side, com hashing de senhas via Werkzeug.
- **Frontend:** HTML5 + Bootstrap 5, ícones do Bootstrap Icons, tipografia Poppins e componentes customizados em CSS.
- **JavaScript:** scripts leves para toasts, filtros, carregamento dinâmico de horários e responsividade (incluídos nos templates).

## Estrutura de diretórios
```
Sistema-Clinico-OFICIAL/
├── main.py                 # Entrada Flask e registro do blueprint principal
├── databaser.py            # Conexão SQLite, criação de tabelas, seeds e utilidades
├── routes/
│   └── user.py             # Regras de negócio, autenticação e rotas de cada perfil
├── templates/              # Templates Jinja2 organizados por página
│   ├── base.html           # Layout mestre com estilos globais e scripts compartilhados
│   ├── telainicial.html    # Landing page em estilo herói
│   ├── login.html, register.html, cadastrarUsuarios.html
│   ├── paciente.html, medico.html
│   ├── recepcionista.html, recep_procedimentos.html, recep_ajustes.html
│   └── agendamentoConsulta.html
└── README.md               # Este documento
```

## Configuração e execução

### Requisitos
- Python 3.10 ou superior.
- Pip e ambiente virtual recomendados.

### Passo a passo
1. **Clonar o repositório**
   ```bash
   git clone https://github.com/<seu-usuario>/Sistema-Clinico-OFICIAL.git
   cd Sistema-Clinico-OFICIAL
   ```
2. **Criar e ativar o ambiente virtual (opcional, porém recomendado)**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Linux/macOS
   .venv\Scripts\activate     # Windows PowerShell
   ```
3. **Instalar dependências**
   ```bash
   pip install -r requirements.txt
   ```
   Caso não exista um `requirements.txt`, instale os pacotes básicos manualmente:
   ```bash
   pip install flask werkzeug
   ```
4. **Inicializar o banco**
   A criação das tabelas e dados padrão ocorre automaticamente ao iniciar o servidor (via `criar_tabelas()` no `main.py`).
5. **Executar o servidor**
   ```bash
   python main.py
   ```
   O Flask abrirá em `http://127.0.0.1:5000/` por padrão.

## Dados iniciais e cadastros
- Procedimentos iniciais: "Consulta Particular", "Consulta Convênio" e "Solicitação de Receita".
- Salas iniciais: Sala 1, Sala 2 e Sala 3.
- Usuário semente: `recepcionistamaster@gmail.com` (senha `12345`), com privilégios de recepcionista master.
- Novos pacientes se cadastram pela tela de registro. Médicos e outros perfis podem ser cadastrados via recepção.

## Fluxo de agendamento e ajustes
1. **Recepção** agenda consultas escolhendo paciente, médico, procedimento, sala, data e horário em intervalos de 30 minutos.
2. Antes de confirmar, o sistema checa conflitos na combinação sala/data/horário e só permite horários livres.
3. Pacientes podem solicitar alteração de horário; a interface exibe apenas slots vagos para o mesmo médico e sala.
4. Recepcionistas avaliam solicitações de ajuste, aceitando ou negando, e os status são propagados para todas as visões.
5. Agendamentos acompanham status em tempo real (agendado, em atendimento, concluído, cancelado), com badges coloridas.

## Estilos e responsividade
- Layout baseado em cartões com transparência e sombras suaves, seguindo a paleta azul indicada.
- Componentes reutilizáveis para botões, badges e formulários garantem consistência entre páginas.
- Templates utilizam breakpoints do Bootstrap para reorganizar colunas e cards no mobile.
- Scripts JavaScript ativam toasts automáticos, filtros instantâneos e comportamentos mobile-friendly (ex.: offcanvas).

## Testes rápidos
O projeto inclui um "sanity check" simples para validar a compilação dos arquivos Python:
```bash
python -m compileall .
```
Se desejar ampliar a cobertura de testes, recomenda-se adicionar testes unitários com `pytest` e cenários de integração para as rotas principais.

## Dicas para evolução
- Adicionar envio de e-mails ou notificações push ao confirmar/alterar consultas.
- Criar exportação de relatórios (PDF/Excel) a partir da agenda diária.
- Incluir controle de acesso baseado em permissões mais granulares para recepcionistas.
- Externalizar configurações sensíveis (como `secret_key`) para variáveis de ambiente.

## Direitos autorais
Este sistema é de uso exclusivo e pessoal do criador original. Qualquer uso, reprodução ou derivação requer autorização prévia por escrito. Respeite os direitos autorais.
