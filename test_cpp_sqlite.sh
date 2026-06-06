#!/usr/bin/env bash
# Test: run doxygen-index project against ../cpp-sqlite
#
# Usage:
#   ./test_cpp_sqlite.sh           # JSON output (default)
#   ./test_cpp_sqlite.sh --neo4j   # JSON + Neo4j ingestion + verification
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CPP_SQLITE_DIR="$SCRIPT_DIR/../cpp-sqlite"
CONFIG_FILE="$CPP_SQLITE_DIR/.doxygen-index.toml"
OUTPUT_JSON="$CPP_SQLITE_DIR/build/docs/doxygen-cpp_sqlite/cpp_sqlite.json"

# ---- Parse flags ----
NEO4J=false
NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-msd-local-dev}"

for arg in "$@"; do
    case "$arg" in
        --neo4j) NEO4J=true ;;
        --neo4j-uri=*) NEO4J_URI="${arg#*=}" ;;
        *) echo "Unknown flag: $arg"; exit 1 ;;
    esac
done

# ---- 1. Config file ----
echo "=== 1. Ensure config file exists ==="
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Creating $CONFIG_FILE ..."
    cat > "$CONFIG_FILE" <<'TOML'
[project]
name = "cpp_sqlite"
input_paths = ["cpp_sqlite/src"]
file_patterns = "*.h *.hpp *.cpp"
exclude_patterns = "*/test/* */build/* */.git/*"
predefined = "BOOST_DESCRIBE_CPP14=1 SQLITE_USECPP20=1 FMT_CONSTEVAL="
TOML
    echo "Created."
else
    echo "Config found: $CONFIG_FILE"
fi

# ---- 2. Run doxygen-index project ----
echo ""
echo "=== 2. Run doxygen-index project ==="

if $NEO4J; then
    doxygen-index project "$CPP_SQLITE_DIR" \
        --format neo4j \
        --neo4j-uri "$NEO4J_URI" \
        --neo4j-user "$NEO4J_USER" \
        --neo4j-password "$NEO4J_PASSWORD"
else
    doxygen-index project "$CPP_SQLITE_DIR"
fi
EXIT_CODE=$?

# ---- 3. Verify JSON output ----
echo ""
echo "=== 3. Verify JSON output ==="
if [ -f "$OUTPUT_JSON" ]; then
    echo "JSON output found: $OUTPUT_JSON"
    python3 -c "
import json
d = json.load(open('$OUTPUT_JSON'))
print(f\"  metadata.source:   {d['metadata']['source']}\")
print(f\"  classes:           {len(d['classes'])}\")
print(f\"  methods:           {len(d['methods'])}\")
print(f\"  functions:         {len(d['functions'])}\")
print(f\"  namespaces:        {len(d['namespaces'])}\")
print(f\"  files:             {len(d['files'])}\")
print(f\"  includes:          {len(d['includes'])}\")
print(f\"  invokes:           {len(d['invokes'])}\")
layer = d['classes'][0].get('layer', 'N/A') if d['classes'] else 'N/A'
print(f\"  layer:             {layer}\")
print(f\"  format_version:    {d['metadata']['format_version']}\")
print()
print('First 3 class names:')
for c in d['classes'][:3]:
    print(f\"    {c['qualified_name']} ({c['kind']})\")
print()
print('First 3 method names:')
for m in d['methods'][:3]:
    print(f\"    {m['qualified_name']}\")
"
else
    echo "❌ Output not found at $OUTPUT_JSON"
    exit 1
fi

# ---- 4. Verify Neo4j (only with --neo4j) ----
if $NEO4J; then
    echo ""
    echo "=== 4. Verify Neo4j ingestion ==="

    if ! python3 -c "import neo4j" 2>/dev/null; then
        echo "⚠ neo4j package not installed. Skipping Neo4j verification."
    else
        echo "Neo4j URI: $NEO4J_URI"
        python3 -c "
from neo4j import GraphDatabase
import sys

uri = '$NEO4J_URI'
user = '$NEO4J_USER'
pw = '$NEO4J_PASSWORD'

try:
    driver = GraphDatabase.driver(uri, auth=(user, pw))
    driver.verify_connectivity()
    print(f'  Connected OK')
except Exception as e:
    print(f'  ❌ Connection failed: {e}')
    sys.exit(1)

with driver.session() as s:
    # Node counts by label (show all labels per node)
    res = s.run('''
        MATCH (n)
        WHERE n.source = 'cpp_sqlite'
        WITH labels(n) AS lbls
        RETURN lbls, count(*) AS cnt
        ORDER BY cnt DESC
    ''')
    print()
    print('  Node counts by label (source=cpp_sqlite):')
    for r in res:
        print(f'    {str(r[\"lbls\"]):40s} {r[\"cnt\"]}')

    # Relationship counts
    res = s.run('''
        MATCH (n)-[r]->()
        WHERE n.source = 'cpp_sqlite'
        WITH type(r) AS rel
        RETURN rel, count(*) AS cnt
        ORDER BY rel
    ''')
    print()
    print('  Relationship counts:')
    for r in res:
        print(f'    {r[\"rel\"]:20s} {r[\"cnt\"]}')

    # Sample: list classes via _CompoundMixin
    res = s.run('''
        MATCH (c:_CompoundMixin {source: \"cpp_sqlite\"})
        RETURN c.qualified_name AS name, c.kind AS kind
        ORDER BY name
        LIMIT 5
    ''')
    print()
    print('  First 5 classes in Neo4j:')
    for r in res:
        print(f'    {r[\"name\"]} ({r[\"kind\"]})')

    # Sample: search members by name substring
    res = s.run('''
        MATCH (m:_MemberMixin)
        WHERE m.source = \"cpp_sqlite\" AND m.name CONTAINS \"insert\"
        RETURN m.name AS name, m.qualified_name AS qname
        LIMIT 5
    ''')
    print()
    print('  Members containing \"insert\":')
    for r in res:
        print(f'    {r[\"name\"]:30s} {r[\"qname\"]}')

    # Count MethodNodes specifically
    res = s.run('''
        MATCH (m:MethodNode {source: \"cpp_sqlite\"})
        RETURN count(m) AS cnt
    ''')
    print()
    print(f'  MethodNode count: {res.single()[\"cnt\"]}')

driver.close()
print()
print('  ✅ Neo4j verification passed')
"
    fi
fi

echo ""
echo "✅ ALL CHECKS PASSED"
exit $EXIT_CODE
