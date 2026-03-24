#!/bin/bash
# setup_resid.sh - Download and compile the reSID library on macOS
set -e

# Repository URL
RESID_REPO="https://github.com/cyberplant/resid.git"
RESID_DIR="resid"

confirm() {
    read -p "$1 [y/N]: " response
    case "$response" in
        [yY][eE][sS]|[yY]) 
            true
            ;;
        *)
            false
            ;;
    esac
}

# Check for prerequisites
for cmd in git make xcrun brew; do
    if ! command -v $cmd &> /dev/null; then
        echo "Error: $cmd is not installed. Please install it first."
        exit 1
    fi
done

if ! command -v autoreconf &> /dev/null; then
    echo "autoreconf (part of autoconf/automake) is not installed."
    if confirm "Would you like to install autoconf, automake, and libtool using Homebrew?"; then
        echo "Installing dependencies..."
        brew install autoconf automake libtool
    else
        echo "Cannot proceed without autoreconf. Exiting."
        exit 1
    fi
fi

# Clone or update the repository
if [ ! -d "$RESID_DIR" ]; then
    if confirm "Clone reSID repository from $RESID_REPO?"; then
        echo "Cloning reSID repository..."
        git clone "$RESID_REPO" "$RESID_DIR"
    else
        echo "Cloning cancelled. Exiting."
        exit 1
    fi
else
    if confirm "reSID directory already exists. Update it?"; then
        echo "Updating reSID..."
        cd "$RESID_DIR"
        git pull
        cd ..
    fi
fi

echo "Building reSID..."
cd "$RESID_DIR"

# Generate build scripts
echo "Running autoreconf..."
autoreconf -i

# macOS-specific configuration (as per README.macOS.md)
SDK_PATH=$(xcrun --show-sdk-path)
echo "Using SDK Path: $SDK_PATH"

# Note: The README suggests these CXXFLAGS for modern macOS (Xcode 26+)
# to ensure C++ headers are found correctly.
CXXFLAGS="-I${SDK_PATH}/usr/include/c++/v1 -g -Wall -O3 -fno-exceptions"

echo "Configuring with CXXFLAGS: $CXXFLAGS"
./configure CXXFLAGS="$CXXFLAGS"

echo "Running make..."
make

RESID_ABS_PATH=$(pwd)
echo "reSID library built successfully in $RESID_ABS_PATH"
echo "Static library: $RESID_ABS_PATH/libresid.a"
echo ""

cd ..

if confirm "Would you like to build the Python wrapper now?"; then
    echo "Building the wrapper..."
    make RESID_SRCDIR="$RESID_ABS_PATH"
    echo "Wrapper built successfully."

    if confirm "Would you like to install the wrapper to the project root (../../)?"; then
        make install
        echo "Wrapper installed to project root."
    fi
else
    echo "Skipping wrapper build. You can build it later with:"
    echo "make RESID_SRCDIR=$RESID_ABS_PATH"
fi
