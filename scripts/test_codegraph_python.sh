#!/usr/bin/env bash
# Test: parse codegraph source with PythonParser and load into Neo4j
#
# Usage:
#   ./test_codegraph_python.sh           # Parse + verify, no Neo4j
#   ./test_codegraph_python.sh --neo4j   # Clear DB → ingest → verify
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR/.."
CODEGRAPH_SRC="$PROJECT_ROOT/../codegraph/src"
SOURCE="codegraph"

# ---- Parse flags ----
NEO4J=false
for arg in "$@"; do
    case "$arg" in
        --neo4j) NEO4J=true ;;
        *) echo "Unknown flag: $arg"; exit 1 ;;
    esac
done

# ---- 1. Parse ----
echo "=== 1. Parse codegraph source with PythonParser ==="
if [ ! -d "$CODEGRAPH_SRC" ]; then
    echo "❌ codegraph source not found at $CODEGRAPH_SRC"
    exit 1
fi

RESULT=$(python3 -c "
import sys, json
sys.path.insert(0, '$SCRIPT_DIR/../src')
from doxygen_index.parser import parse_python_dir
result = parse_python_dir('$CODEGRAPH_SRC', source='$SOURCE', progress_interval=0)
print(json.dumps({
    'files': len(result.files),
    'namespaces': len(result.namespaces),
    'classes': len(result.classes),
    'interfaces': len(result.interfaces),
    'enums': len(result.enums),
    'methods': len(result.methods),
    'functions': len(result.functions),
    'attributes': len(result.attributes),
    'parameters': len(result.parameters),
    'includes': len(result.includes),
    'class_names': [c.qualified_name for c in result.classes],
    'method_names': [m.qualified_name for m in result.methods],
    'func_names': [f.qualified_name for f in result.functions],
}))
")
EXIT_CODE=$?

echo "$RESULT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f\"  Files:         {d['files']}\")
print(f\"  Namespaces:    {d['namespaces']}\")
print(f\"  Classes:       {d['classes']}\")
print(f\"  Interfaces:    {d['interfaces']}\")
print(f\"  Enums:         {d['enums']}\")
print(f\"  Methods:       {d['methods']}\")
print(f\"  Functions:     {d['functions']}\")
print(f\"  Attributes:    {d['attributes']}\")
print(f\"  Parameters:   {d['parameters']}\")
print(f\"  Includes:      {d['includes']}\")
"

if [ $EXIT_CODE -ne 0 ]; then
    echo "❌ Parse failed"
    exit $EXIT_CODE
fi

# ---- 2. Verify ----
echo ""
echo "=== 2. Verify expected symbols ==="
python3 -c "
import sys, json
d = json.loads('''$RESULT''')
issues = []

# Classes
expected = {'codegraph.models.compound.ClassNode',
             'codegraph.models.member.MethodNode',
             'codegraph.graph.LayerGraph',
             'codegraph.repository.GraphRepository'}
for e in expected:
    if e not in d['class_names']:
        issues.append(f'Missing class: {e}')

# Methods
expected_m = {'codegraph.models.tags.CodeGraphNode.serialize',
               'codegraph.models.tags.CodeGraphNode.deserialize',
               'codegraph.graph.LayerGraph.to_neo4j'}
for e in expected_m:
    if e not in d['method_names']:
        issues.append(f'Missing method: {e}')

# Functions
expected_f = {'codegraph.connection.cypher_query',
              'codegraph.connection.verify_connectivity'}
for e in expected_f:
    if e not in d['func_names']:
        issues.append(f'Missing function: {e}')

if issues:
    for i in issues:
        print(f'  ⚠ {i}')
    sys.exit(1)
else:
    print('  ✅ All expected symbols found')
"

# ---- 3. Neo4j (optional) ----
if $NEO4J; then
    echo ""
    echo "=== 3. Clear DB, ingest into Neo4j ==="
    python3 -c "
import sys, os
sys.path.insert(0, '$SCRIPT_DIR/../src')

from dotenv import load_dotenv
load_dotenv()

from neomodel import db
db.set_connection('bolt://neo4j:msd-local-dev@localhost:7687')

from doxygen_index.parser import parse_python_dir
from doxygen_index.neo4j_backend import write_result, ensure_schema, clear_source

# Wipe ALL codegraph data first
clear_source('$SOURCE')

# Parse
result = parse_python_dir('$CODEGRAPH_SRC', source='$SOURCE', progress_interval=0)

# Reinstall schema and write
ensure_schema()
write_result(result)

# Verify
res, _ = db.cypher_query('''
    MATCH (n) WHERE n.source = \$src
    RETURN labels(n)[0] AS label, count(*) AS cnt
    ORDER BY cnt DESC
''', {'src': '$SOURCE'})
total = 0
for r in res:
    print(f'  {r[0]:20s} {r[1]}')
    total += r[1]
print(f'  {\"Total\":20s} {total}')

if total == 0:
    sys.exit(1)
" 2>&1 | grep -v '^Found codegraph\|^ + Creating\|^\s*{neo4j_code'
fi

echo ""
echo "✅ ALL CHECKS PASSED"