# -*- coding: utf-8 -*-
"""Wrapper retrocompatível que redireciona para o pipeline único em build_local.py."""

import sys
import build_local

if __name__ == "__main__":
    build_local.main()
