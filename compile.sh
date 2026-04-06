#!/bin/bash
# Compile BASIC programs to PRG format using petcat (VICE tool)
# Usage: ./compile.sh

set -e

BASE_DIR="`dirname $0`"
SOURCES_DIR="$BASE_DIR/src/"
OUTPUT_DIR="$BASE_DIR/programs/"

# Check if petcat is available
if ! command -v petcat &> /dev/null; then
    echo "Error: petcat not found. Please install VICE emulator tools."
    echo "  macOS: brew install vice"
    echo "  Linux: apt install vice"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

# Compile all .BAS files in src directory
for bas_file in "$SOURCES_DIR"/*.BAS; do
    if [ -f "$bas_file" ]; then
        filename=$(basename "$bas_file" .BAS)
        lowercase_name=$(echo "$filename" | tr '[:upper:]' '[:lower:]')
        output_file="$OUTPUT_DIR/${lowercase_name}.prg"
        
        echo "Compiling $bas_file -> $output_file"
        petcat -w2 -o "$output_file" -- "$bas_file"
    fi
done

echo "Done! Programs are in $OUTPUT_DIR"
