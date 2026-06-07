#!/bin/bash

# Script to run all VICE tests and save results with timestamp
# Usage: ./tools/run_tests.sh [options]
# Options:
#   --quick     Run only problematic tests (faster)
#   --full      Run all VICE tests (default)
#   --vice      Run only original test/test_vice.py tests
#   --all       Run both test suites

# Note: do NOT use set -e here, test failures are expected and handled

# Configuration
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
RESULTS_DIR="test_results"
LOG_FILE="${RESULTS_DIR}/c64py_test_${TIMESTAMP}.log"
JSON_FILE="${RESULTS_DIR}/c64py_test_${TIMESTAMP}.json"
SUMMARY_FILE="${RESULTS_DIR}/c64py_test_${TIMESTAMP}.txt"

# Detect number of CPU cores
NUM_CPUS=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo "4")
# Use all cores but leave 1 free for system
PARALLEL_JOBS=$((NUM_CPUS > 1 ? NUM_CPUS - 1 : 1))

# Create results directory
mkdir -p "$RESULTS_DIR"

ensure_vice_assets() {
    if [ -n "$(rg --files test/vice -g '*.prg' 2>/dev/null)" ]; then
        return 0
    fi
    echo "VICE assets missing. Fetching pinned corpus..."
    ./scripts/fetch_vice_tests.sh
}

# Function to print header
print_header() {
    echo "======================================"
    echo "VICE Test Suite Runner"
    echo "Timestamp: $TIMESTAMP"
    echo "======================================"
    echo ""
}

# Function to run tests and capture results
run_test_suite() {
    local test_name="$1"
    local test_command="$2"
    local description="$3"
    local use_parallel="$4"
    
    echo "Running: $description"
    echo "Command: $test_command"
    if [ "$use_parallel" = "true" ]; then
        echo "Parallel jobs: $PARALLEL_JOBS (detected $NUM_CPUS CPUs)"
    fi
    echo "----------------------------------------"
    
    # Add parallel flag if requested
    if [ "$use_parallel" = "true" ]; then
        test_command="$test_command -n $PARALLEL_JOBS"
    fi
    
    # Run tests and capture both stdout and results
    eval "$test_command" 2>&1 | tee -a "$LOG_FILE"
    local test_exit_code=${PIPESTATUS[0]}
    
    if [ $test_exit_code -eq 0 ]; then
        echo "✅ $test_name: PASSED" | tee -a "$LOG_FILE"
        return 0
    else
        echo "❌ $test_name: FAILED (exit code: $test_exit_code)" | tee -a "$LOG_FILE"
        return 1
    fi
}

# Function to generate summary
generate_summary() {
    echo "Generating summary..."
    
    cat > "$SUMMARY_FILE" << EOF
VICE Test Suite Summary
======================
Run timestamp: $TIMESTAMP
Log file: $LOG_FILE
JSON results: $JSON_FILE

Test Results:
------------

EOF
    
    # Extract test results from log
    if grep -q "test session starts" "$LOG_FILE"; then
        echo "Pytest Results:" >> "$SUMMARY_FILE"
        grep -E "(passed|failed|error|warnings)" "$LOG_FILE" | tail -5 >> "$SUMMARY_FILE"
        echo "" >> "$SUMMARY_FILE"
    fi
    
    # Extract failed tests
    if grep -q "FAILED" "$LOG_FILE"; then
        echo "Failed Tests:" >> "$SUMMARY_FILE"
        grep "FAILED" "$LOG_FILE" | sed 's/.*::/  - /' >> "$SUMMARY_FILE"
        echo "" >> "$SUMMARY_FILE"
    fi
    
    # Extract stuck programs
    if grep -q "PC stuck at" "$LOG_FILE"; then
        echo "Programs that got stuck:" >> "$SUMMARY_FILE"
        grep "PC stuck at" "$LOG_FILE" | sed 's/.*PC stuck at/  - PC stuck at/' | sort | uniq >> "$SUMMARY_FILE"
        echo "" >> "$SUMMARY_FILE"
    fi
    
    # System info
    echo "System Information:" >> "$SUMMARY_FILE"
    echo "  Python: $(python3 --version)" >> "$SUMMARY_FILE"
    echo "  OS: $(uname -s -r)" >> "$SUMMARY_FILE"
    echo "  Date: $(date)" >> "$SUMMARY_FILE"
    
    echo "Summary saved to: $SUMMARY_FILE"
}

# Main execution
main() {
    local test_mode="${1:-full}"
    local use_parallel="false"
    
    # Check for parallel flag
    if [[ "$2" == "--parallel" || "$1" == "--parallel" ]]; then
        use_parallel="true"
        # Shift arguments if --parallel was first
        if [[ "$1" == "--parallel" ]]; then
            test_mode="${2:-full}"
        fi
    fi
    
    print_header | tee "$LOG_FILE"
    ensure_vice_assets
    
    echo "Results will be saved to:"
    echo "  Log: $LOG_FILE"
    echo "  JSON: $JSON_FILE"
    echo "  Summary: $SUMMARY_FILE"
    echo "  Parallel execution: $use_parallel (using $PARALLEL_JOBS jobs)"
    echo "" | tee -a "$LOG_FILE"
    
    # Track overall success
    local overall_success=0
    
    case "$test_mode" in
        --quick)
            echo "Running quick test suite (problematic programs only)..."
            run_test_suite "Quick" ".venv/bin/python -m pytest test/test_all_vice.py -m quick -v --tb=short" "Quick VICE Tests (subset)" "$use_parallel"
            overall_success=$?
            ;;
        --vice)
            echo "Running original VICE test suite..."
            run_test_suite "VICE" ".venv/bin/python -m pytest test/test_vice.py -v --tb=short" "Original VICE Tests" "$use_parallel"
            overall_success=$?
            ;;
        --full)
            echo "Running full VICE test suite (this may take a while)..."
            run_test_suite "AllVICE" ".venv/bin/python -m pytest test/test_all_vice.py -v --tb=short" "All VICE Programs" "$use_parallel"
            overall_success=$?
            ;;
    esac
    
    echo "" | tee -a "$LOG_FILE"
    echo "======================================" | tee -a "$LOG_FILE"
    
    if [ $overall_success -eq 0 ]; then
        echo "✅ All tests completed successfully!" | tee -a "$LOG_FILE"
    else
        echo "❌ Some tests failed. Check the log for details." | tee -a "$LOG_FILE"
    fi
    
    echo "Results saved to: $RESULTS_DIR/" | tee -a "$LOG_FILE"
    
    # Copy JSON results if they exist (get the most recent by modification time)
    latest_json=$(ls -t logs/vice_test_results_*.json 2>/dev/null | head -1)
    if [ -f "$latest_json" ]; then
        cp "$latest_json" "$JSON_FILE" 2>/dev/null || true
        echo "Copied JSON results from: $latest_json" | tee -a "$LOG_FILE"
        
        # If parallel execution was used, try to merge multiple JSON files
        if [ "$use_parallel" = "true" ]; then
            echo "Parallel execution detected - checking for multiple JSON files to merge..." | tee -a "$LOG_FILE"
            
            # Find all JSON files with the same timestamp pattern
            timestamp_pattern=$(echo "$latest_json" | sed 's/.*vice_test_results_\([0-9]*_[0-9]*\)_.*\.json/\1/')
            json_files=(logs/vice_test_results_${timestamp_pattern}*.json)
            
            # Sort by modification time to get the most recent files
            IFS=$'\n' json_files=($(ls -t "${json_files[@]}" 2>/dev/null))
            
            if [ ${#json_files[@]} -gt 1 ]; then
                echo "Found ${#json_files[@]} JSON files to merge" | tee -a "$LOG_FILE"
                
                # Create a Python script to merge JSON files
                cat > /tmp/merge_json.py << 'EOF'
import json
import sys
from pathlib import Path

def merge_json_files(json_files, output_file):
    """Merge multiple JSON test result files."""
    merged = {
        "timestamp": "",
        "max_cycles": 5000000,
        "tests": {}
    }
    
    total_tests = 0
    
    for json_file in json_files:
        try:
            with open(json_file, 'r') as f:
                data = json.load(f)
                
            # Use the latest timestamp
            if data.get("timestamp"):
                merged["timestamp"] = data["timestamp"]
            
            # Use max_cycles from first file (they should be the same)
            if data.get("max_cycles") and merged["max_cycles"] == 5000000:
                merged["max_cycles"] = data["max_cycles"]
            
            # Merge tests
            if "tests" in data:
                for test_name, test_result in data["tests"].items():
                    merged["tests"][test_name] = test_result
                    total_tests += 1
                    
        except Exception as e:
            print(f"Error processing {json_file}: {e}")
            continue
    
    # Write merged results
    with open(output_file, 'w') as f:
        json.dump(merged, f, indent=2)
    
    return total_tests

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python merge_json.py output.json input1.json input2.json ...")
        sys.exit(1)
    
    output_file = sys.argv[1]
    input_files = sys.argv[2:]
    
    total_tests = merge_json_files(input_files, output_file)
    print(f"Merged {len(input_files)} JSON files with {total_tests} total tests")
EOF
                
                # Run the merge script
                .venv/bin/python /tmp/merge_json.py "$JSON_FILE" "${json_files[@]}" | tee -a "$LOG_FILE"
                rm -f /tmp/merge_json.py
                
            else
                echo "Only one JSON file found, no merge needed" | tee -a "$LOG_FILE"
            fi
        fi
        
    else
        echo "No JSON results found to copy" | tee -a "$LOG_FILE"
    fi
    
    # Generate summary
    generate_summary
    
    # Display summary
    echo ""
    echo "Quick Summary:"
    cat "$SUMMARY_FILE"
    
    return $overall_success
}

# Show usage if no arguments or --help
if [[ -z "$1" || "$1" == "--help" || "$1" == "-h" ]]; then
    echo "Usage: $0 [option] [--parallel]"
    echo ""
    echo "Options:"
    echo "  --quick         Run only problematic tests (faster)"
    echo "  --full          Run all VICE tests (default)"
    echo "  --vice          Run only original test/test_vice.py tests"
    echo "  --parallel      Run tests in parallel using multiple CPU cores"
    echo "  --help          Show this help"
    echo ""
    echo "Examples:"
    echo "  $0 --quick              Quick test (problematic programs only)"
    echo "  $0 --full               Full test suite (sequential)"
    echo "  $0 --full --parallel    Full test suite (parallel, faster)"
    echo "  $0 --vice --parallel    Original VICE tests (parallel)"
    echo ""
    echo "Results are saved to test_results/ with timestamp:"
    echo "  - c64py_test_YYYYMMDD_HHMMSS.log  (detailed log)"
    echo "  - c64py_test_YYYYMMDD_HHMMSS.json (test results JSON)"
    echo "  - c64py_test_YYYYMMDD_HHMMSS.txt  (human readable summary)"
    exit 0
fi

# Run main function and exit with its exit code
main "$@"
exit $?
