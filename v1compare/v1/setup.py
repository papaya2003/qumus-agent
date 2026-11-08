from setuptools import setup, find_packages

setup(
    name="Qumus",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "smolagents",
        "litellm",
        "fastapi",
        "uvicorn",
        "sqlalchemy",
        "pyserial",
        "pydantic",
        "requests",
        "python-multipart",
        "opencv-python",
        "python-dotenv",
        "PyMuPDF",
        "claude-agent-sdk",
    ],
)
