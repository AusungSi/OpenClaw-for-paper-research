# Alembic Migrations

Initialize database schema with:

```powershell
alembic upgrade head
```

Create a new migration after model changes:

```powershell
alembic revision --autogenerate -m "describe_change"
```

