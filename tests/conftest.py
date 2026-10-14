import os
import secrets

# Test imports must never load the project's real .env or contact its RDS.
os.environ['APP_ENV']='test'
os.environ['DB_ENGINE']='sqlite'
os.environ['JWT_SECRET']=secrets.token_urlsafe(48)
