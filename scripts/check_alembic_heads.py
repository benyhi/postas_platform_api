from alembic.config import Config
from alembic.script import ScriptDirectory


heads = ScriptDirectory.from_config(Config('alembic.ini')).get_heads()
if len(heads) != 1:
    raise SystemExit(f'Expected exactly one Alembic head, found {len(heads)}.')

print(f'Alembic migration head is unique: {heads[0]}')
