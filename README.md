# RodiumAI Bootcamp : session 4

## Lancer le serveur

Toutes les commandes se lancent depuis la racine du projet.

```bash
uv sync                     
cp .env.example .env  # Et mettre à jour les variables environement (modèle et clé api)   
uv run alembic upgrade head
uv run fastapi dev main.py
```

## Utiliser PostgreSQL au lieu de SQLite

Grâce à SQLAlchemy, seule la connexion change : `models.py`, `main.py` et les migrations Alembic restent identiques.

1. Installer le driver PostgreSQL :

```bash
uv add "psycopg[binary]"
```

2. `database/db.py` devient :

```python
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

# Format : postgresql+psycopg://<utilisateur>:<mot_de_passe>@<hôte>:<port>/<base>
DATABASE_URL = os.environ["DATABASE_URL"]

# pool_pre_ping : vérifie qu'une connexion est toujours ouverte avant de la réutiliser
# (un serveur PostgreSQL peut fermer les connexions inactives, contrairement à un fichier SQLite).
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(engine)


class Base(DeclarativeBase):
    pass


def get_db():
    # FastAPI dependency: one session per request, closed once the response is sent.
    with SessionLocal() as db:
        yield db
```

3. Dans `.env` :

```
DATABASE_URL=postgresql+psycopg://chatbot:chatbot@localhost:5432/chatbot
```

4. Créer les tables dans PostgreSQL (la base doit déjà exister) :

```bash
uv run alembic upgrade head
```

## Fonctionnalité 1 : Rôle personnalisé `note`

Study Buddy prend en charge un rôle de message personnalisé appelé `note`, qui permet à l'étudiant d'enregistrer des notes personnelles directement dans une conversation.

### Fonctionnement

* **Enregistrement en base de données :** les notes sont enregistrées comme les autres messages, avec leur rôle `note`, leur contenu et leur ordre dans la conversation.
* **Affichage dans l'interface :** les notes apparaissent dans le chat avec un style visuel distinct afin de les différencier des messages de l'étudiant, des réponses de l'assistant et des notifications système.
* **Création d'une note :** l'étudiant clique sur « Ajouter une note », saisit son contenu puis l'enregistre. La note est associée à la conversation active.
* **Historique envoyé au LLM :** les messages dont le rôle est `note` sont exclus de l'historique transmis au modèle de langage.

### Justification du choix

Le rôle `note` est destiné à conserver des informations personnelles, des rappels ou des notions à retenir. Ces notes ne constituent pas des messages adressés à l'assistant. Nous avons donc choisi de les filtrer lors de la construction de l'historique envoyé au LLM, au même titre que les notifications système.

Ce choix permet de conserver les notes dans la conversation et en base de données sans les transmettre au modèle. Les messages utilisateur et assistant restent, quant à eux, disponibles pour construire le contexte de la conversation.

