#!/usr/bin/env python3
"""
Backward compatibility entry point for Kroger MCP server
"""

import os
import sys

# Add src directory to path for development
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from kroger_mcp.server import main

if __name__ == "__main__":
    main()
