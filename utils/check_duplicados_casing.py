"""Script para correr LOCALMENTE contra la DB de producción real (clientes_v2.db).
Detecta empresas con mismo nombre normalizado (lower) pero distinto registro."""
import sqlite3, sys

db_path = sys.argv[1] if len(sys.argv) > 1 else "clientes_v2.db"
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

rows = conn.execute("SELECT id, nombre, pais FROM empresas").fetchall()
grupos = {}
for r in rows:
    key = r["nombre"].strip().lower()
    grupos.setdefault(key, []).append((r["id"], r["nombre"], r["pais"]))

dups = {k: v for k, v in grupos.items() if len(v) > 1}

print(f"Total empresas: {len(rows)}")
print(f"Grupos con duplicado case-insensitive: {len(dups)}")
total_dup_rows = sum(len(v) for v in dups.values())
print(f"Registros involucrados: {total_dup_rows}")
print()
for k, v in sorted(dups.items()):
    print(f"  '{k}':")
    for (id_, nombre, pais) in v:
        # contar cotizaciones de cada uno
        n_cot = conn.execute(
            "SELECT COUNT(*) FROM cotizaciones WHERE empresa_id=?", (id_,)
        ).fetchone()[0]
        print(f"    id={id_:<6} nombre={nombre!r:<30} pais={pais!r:<15} cotizaciones={n_cot}")
