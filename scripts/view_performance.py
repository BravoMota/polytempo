#!/usr/bin/env python3
"""Streamlit entrypoint for the paper performance viewer.

Usage:
    streamlit run scripts/view_performance.py -- reports/performance/daily.csv
"""

from polytempo.visualizer.app import main

main()
