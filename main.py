from flask import Flask, render_template
from databaser import criar_tabelas
from routes.user import user_bp

# garante estrutura + seeds idempotentes
criar_tabelas()

main = Flask(__name__)
main.secret_key = 'minha_chave_super_secreta_123'  # troque em produção

@main.route('/')
def telaInicial():
    return render_template('telainicial.html')

# endpoints ficam com o prefixo user.*
main.register_blueprint(user_bp, url_prefix='/user')

if __name__ == '__main__':
    main.run(host='0.0.0.0', port=5000, debug=True)
