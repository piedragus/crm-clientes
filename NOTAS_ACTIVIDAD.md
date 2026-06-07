# Feature: Notas y Actividad por Empresa

## Objetivo
Registrar llamadas, emails, reuniones y notas libres vinculadas
a cada empresa. Es la feature más pedida en CRMs.

## Schema propuesto
```sql
CREATE TABLE actividades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa_id  INTEGER NOT NULL,
    fecha       TEXT NOT NULL,
    tipo        TEXT,   -- llamada | email | reunion | nota
    texto       TEXT,
    usuario     TEXT,
    FOREIGN KEY (empresa_id) REFERENCES empresas(id) ON DELETE CASCADE
)
```

## Endpoints a agregar
- GET  /api/empresas/:id/actividades
- POST /api/empresas/:id/actividades
- PUT  /api/actividades/:id
- DELETE /api/actividades/:id

## UI
- Tercer tab en panel detalle: "Actividad"
- Timeline vertical con íconos por tipo
- Input rápido: tipo + texto + Enter

## Estado: PENDIENTE
