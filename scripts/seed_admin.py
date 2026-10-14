"""Run explicitly: python -m scripts.seed_admin. The only way to create an admin account.

Run after scripts.seed so departments exist. The password comes from ADMIN_PASSWORD
or an interactive prompt and is never stored in code.
"""
import getpass
import os

from dotenv import load_dotenv
from core.config import ROOT
from core.database import Database
from core.security import hash_password
from repositories import users
from schemas.auth import RegisterRequest


def seed_admin(db,*,email,name,password,department_code):
    department=db.query('SELECT id FROM departments WHERE code=%s',(department_code,),one=True)
    if department is None:
        raise ValueError(f'Department {department_code} does not exist; run python -m scripts.seed first')
    # Same validation as registration: email format, name and password length.
    account=RegisterRequest(name=name,email=email,password=password,department_id=department['id'])
    if db.query('SELECT id FROM users WHERE email=%s',(account.email,),one=True):
        raise ValueError(f'{account.email} already exists; existing accounts are never changed')
    return users.create(db,email=account.email,name=account.name,password_hash=hash_password(account.password),
                        department_id=account.department_id,role='admin')


if __name__=='__main__':
    load_dotenv(ROOT/'.env')
    email=os.getenv('ADMIN_EMAIL') or input('Admin email: ')
    name=os.getenv('ADMIN_NAME') or input('Admin name: ')
    department=os.getenv('ADMIN_DEPARTMENT') or input('Department code (e.g. ADM): ')
    password=os.getenv('ADMIN_PASSWORD') or getpass.getpass('Admin password (8+ characters): ')
    seed_admin(Database.from_env(),email=email,name=name,password=password,department_code=department.strip().upper())
    print(f'Admin account created: {email}')
