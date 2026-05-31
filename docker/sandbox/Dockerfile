FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-noto-cjk \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel
RUN python -m pip install --no-cache-dir \
    beautifulsoup4 \
    charset-normalizer \
    lxml \
    matplotlib \
    numpy \
    openpyxl \
    pandas \
    pdfplumber \
    pillow \
    pypdf \
    python-docx \
    python-pptx \
    reportlab \
    requests \
    scipy \
    seaborn \
    tabulate \
    xlsxwriter \
    xlrd

WORKDIR /work

CMD ["python", "--version"]
