# creates the app and can include a config.py
from flask import Flask
from celery import Celery
from flask.ext.login import LoginManager
from flask.ext.mysql import MySQL
import os


def create_app(config=None, app_name=None, blueprints=None):
    app = Flask(__name__)
    return app

path = os.path.abspath('config.py')
os.chdir(path[:-9])

app = create_app()
app.config.from_object('config')
print os.getcwd()

celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)


login_manager = LoginManager()
login_manager.init_app(app)

mysql = MySQL()

# mysql configurations


mysql.init_app(app)

from . import auth
