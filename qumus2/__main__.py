from qumus2.cli import main

# Guarded: worker processes started with "spawn" (the scan-tile recognition
# pool) import the main module again and must not start another Qumus.
if __name__ == "__main__":
    main()
