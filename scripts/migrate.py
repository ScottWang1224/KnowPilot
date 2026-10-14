"""Run explicitly: python -m scripts.migrate. Never runs during app startup."""
import hashlib
import re

from dotenv import load_dotenv
from core.config import ROOT
from core.database import get_db_connection

HISTORY_DDL = '''CREATE TABLE IF NOT EXISTS schema_migrations (
  version VARCHAR(100) NOT NULL,
  checksum CHAR(64) NOT NULL,
  applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''


def normalize_type(value):
    return re.sub(r'\b(bigint|int)\(\d+\)',r'\1',value.lower()).replace('boolean','tinyint(1)')


def normalize_default(value):
    if value is None: return None
    value=str(value)
    if value.upper() in {'TRUE','FALSE'}: return '1' if value.upper()=='TRUE' else '0'
    if value.lower() in {'current_timestamp','current_timestamp()'}: return 'current_timestamp'
    return value.strip("'")


def ddl_contract(sql):
    """Parse our deliberately simple, one-column-per-line migration dialect."""
    tables=[]
    for match in re.finditer(r'CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\) ENGINE=',sql,re.S):
        name, body=match.groups()
        columns={}
        unique=[]
        foreign=[]
        for line in body.splitlines():
            column=re.match(r"\s+(\w+) ((?:BIGINT|INT) UNSIGNED|VARCHAR\(\d+\)|CHAR\(\d+\)|ENUM\('[^\n]+?'\)|BOOLEAN|DATETIME|LONGTEXT)(.*)",line)
            if column:
                col, kind, rest=column.groups()
                default=re.search(r"\bDEFAULT\s+('[^']*'|[\w()]+)",rest)
                columns[col]=(normalize_type(kind),'NOT NULL' not in rest,
                              normalize_default(default[1]) if default else None,
                              'AUTO_INCREMENT' in rest,'ON UPDATE CURRENT_TIMESTAMP' in rest)
            index=re.search(r'(?:PRIMARY KEY|UNIQUE KEY \w+) \(([^)]+)\)',line)
            if index: unique.append(tuple(part.strip() for part in index[1].split(',')))
            fk=re.search(r'FOREIGN KEY \((\w+)\) REFERENCES (\w+)\((\w+)\)',line)
            if fk: foreign.append(fk.groups())
        if not columns:
            raise RuntimeError(f'No schema contract parsed for {name}')
        tables.append((name,columns,tuple(unique),tuple(foreign)))
    if not tables:
        raise RuntimeError('Migration contains no table definitions')
    return tables


def validate_table(cursor, table, columns, unique, foreign):
    cursor.execute('''SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,EXTRA FROM information_schema.COLUMNS
                      WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s''',(table,))
    actual={r['COLUMN_NAME']:r for r in cursor.fetchall()}
    for name,contract in columns.items():
        kind,nullable=contract[:2]
        row=actual.get(name)
        if not row or normalize_type(row['COLUMN_TYPE']) != kind or (row['IS_NULLABLE']=='YES') != nullable:
            raise RuntimeError(f'Incompatible schema: {table}.{name}; review manually, no data was dropped')
        if len(contract)>2:
            default,identity,on_update=contract[2:]
            extra=row.get('EXTRA','').lower()
            if (normalize_default(row.get('COLUMN_DEFAULT')) != default or
                    ('auto_increment' in extra) != identity or
                    ('on update current_timestamp' in extra) != on_update):
                raise RuntimeError(f'Incompatible schema: {table}.{name} identity/default; review manually')
    for name,row in actual.items():
        if name not in columns and row['IS_NULLABLE']=='NO' and row.get('COLUMN_DEFAULT') is None and 'auto_increment' not in row.get('EXTRA',''):
            raise RuntimeError(f'Incompatible required extra column: {table}.{name}')
    cursor.execute('''SELECT INDEX_NAME,COLUMN_NAME,NON_UNIQUE,SEQ_IN_INDEX FROM information_schema.STATISTICS
                      WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY INDEX_NAME,SEQ_IN_INDEX''',(table,))
    indexes={}
    for row in cursor.fetchall():
        if row['NON_UNIQUE']==0:
            indexes.setdefault(row['INDEX_NAME'],[]).append(row['COLUMN_NAME'])
    if any(index not in [tuple(v) for v in indexes.values()] for index in unique):
        raise RuntimeError(f'Incompatible unique index on {table}')
    cursor.execute('''SELECT COLUMN_NAME,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE
                      WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND REFERENCED_TABLE_NAME IS NOT NULL''',(table,))
    references={(r['COLUMN_NAME'],r['REFERENCED_TABLE_NAME'],r['REFERENCED_COLUMN_NAME']) for r in cursor.fetchall()}
    if any(fk not in references for fk in foreign):
        raise RuntimeError(f'Incompatible foreign key on {table}')


def migrate(connection=None):
    owns_connection=connection is None
    connection=connection or get_db_connection()
    cursor=None
    locked=False
    applied=[]
    try:
        cursor=connection.cursor(dictionary=True)
        cursor.execute("SELECT GET_LOCK(SHA2(CONCAT(DATABASE(), ':smartfab_migrate'),256), 15) AS acquired")
        locked=cursor.fetchone()['acquired']==1
        if not locked:
            raise RuntimeError('Could not acquire migration lock')
        cursor.execute('SELECT VERSION() AS version')
        server_version=cursor.fetchone()['version']
        match=re.match(r'(\d+)\.(\d+)\.(\d+)',server_version)
        if not match or 'mariadb' in server_version.lower() or tuple(map(int,match.groups())) < (8,0,16):
            raise RuntimeError('SmartFab requires MySQL 8.0.16+ for enforced CHECK constraints')
        cursor.execute(HISTORY_DDL)
        for contract in ddl_contract(HISTORY_DDL):
            validate_table(cursor,*contract)
        for path in sorted((ROOT/'migrations').glob('*.sql')):
            sql=path.read_text(encoding='utf-8')
            checksum=hashlib.sha256(sql.encode()).hexdigest()
            cursor.execute('SELECT checksum FROM schema_migrations WHERE version=%s',(path.name,))
            previous=cursor.fetchone()
            if previous and previous['checksum'] != checksum:
                raise RuntimeError(f'Applied migration changed: {path.name}; add a new migration instead')
            if not previous:
                # MySQL DDL commits implicitly. IF NOT EXISTS permits recovery after partial DDL.
                for statement in sql.split(';'):
                    if statement.strip(): cursor.execute(statement)
            for contract in ddl_contract(sql):
                validate_table(cursor,*contract)
            cursor.execute('''SELECT ENGINE,TABLE_COLLATION FROM information_schema.TABLES
                              WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN
                              ('departments','users','announcements','documents','login_attempts','schema_migrations')''')
            if any(r['ENGINE']!='InnoDB' or not r['TABLE_COLLATION'].startswith('utf8mb4') for r in cursor.fetchall()):
                raise RuntimeError('SmartFab tables require InnoDB and utf8mb4')
            cursor.execute('''SELECT ENFORCED FROM information_schema.TABLE_CONSTRAINTS
                              WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='documents'
                              AND CONSTRAINT_NAME='ck_documents_department' AND CONSTRAINT_TYPE='CHECK' ''')
            check=cursor.fetchone()
            if not check or check['ENFORCED']!='YES':
                raise RuntimeError('Missing enforced document department constraint')
            if not previous:
                cursor.execute('INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)',(path.name,checksum))
                connection.commit()
                applied.append(path.name)
        return applied
    except Exception:
        connection.rollback()
        raise
    finally:
        try:
            if locked:
                cursor.execute("SELECT RELEASE_LOCK(SHA2(CONCAT(DATABASE(), ':smartfab_migrate'),256)) AS released")
                cursor.fetchone()
        finally:
            try:
                if cursor is not None: cursor.close()
            finally:
                if owns_connection: connection.close()


if __name__=='__main__':
    load_dotenv(ROOT/'.env')
    result=migrate()
    print('Applied: '+', '.join(result) if result else 'Schema verified; no pending migrations.')
