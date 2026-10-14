import os
import secrets

# Test imports must never load the project's real .env or contact its RDS.
os.environ['APP_ENV']='test'
os.environ['DEMO_LOGIN_ENABLED']='false'
os.environ['SESSION_SECRET']=secrets.token_urlsafe(48)
