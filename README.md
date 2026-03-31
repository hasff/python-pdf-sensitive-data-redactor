# Python PDF Sensitive Data Redactor

Automatically detect and permanently redact sensitive personal information from PDF files — text, embedded images, and metadata — using Python and AI.

*Status: March 2026*

This project is a **production-style case study** of a problem that sounds simple but isn't:

> *"Can't I just ask ChatGPT to redact my PDF?"*

The short answer is no. This repository shows why — and builds the pipeline that actually works.

---

## The problem

Imagine you have a confidential document — a legal case, a medical report, an internal investigation — and you need to share it externally with sensitive information removed.

The document contains:
- Names, addresses, phone numbers, and email addresses in the text
- A table with contact details
- Embedded images that also contain sensitive text
- A handwritten signature
- Document metadata (author, creation date, software used)

You need **true redaction**: the information must be gone from the file, not just hidden behind a black box that anyone can copy-paste from.

---

## What happens when you ask AI chatbots to do it

I tested four major AI assistants with the same document and the same request.

### Gemini and Mistral
Both refused to produce a PDF file at all. They returned the text with `[REDACTED]` markers as plain text, and asked me to paste it into Word and export it myself. Original formatting: lost. Images: ignored. Metadata: untouched.

### Microsoft Copilot
Returned the **original unmodified file** and claimed it had been fully redacted — complete with a detailed list of everything that had supposedly been removed. Nothing was removed.

### ChatGPT
Made the most genuine attempt across three tries. The results:

| Attempt | Result |
|---|---|
| 1 | Produced a PDF but rewrote the document from scratch. No original formatting, no images, simplified text. |
| 2 | Better layout, but left names visible, dropped words mid-sentence, ignored images and the handwritten signature. |
| 3 | Regressed. Produced a 48 MB file with images converted to raw text and redaction incomplete. |

**The core limitation is the same in every case:** AI chatbots can read a document and identify what is sensitive. But they cannot edit a binary PDF file. When they try, they reconstruct the document from scratch — and the reconstruction loses formatting, tables, images, and precision.

The files generated during this comparison are available in the [`llm_comparison/`](llm_comparison/) folder.

---

## Why this is harder than it looks

A PDF is not a Word document. It is a list of low-level drawing instructions: *"place this character at these exact coordinates, in this font, at this size."* There is no concept of a "word" or a "paragraph" — those are reconstructed by software at reading time.

This creates two specific problems:

**1. The false redact trap**

The most common mistake — and one that has caused real legal scandals — is drawing a black rectangle over sensitive text in a PDF viewer, then saving the file. The text is still there underneath. Anyone can remove the rectangle, or simply copy-paste the document into a text editor to extract everything.

**2. Images inside PDFs**

Embedded images are binary data stored separately from the text layer. A name printed inside an image does not exist as text in the PDF — it is pixels. Standard text redaction tools do not touch it.

---

## How this pipeline works

Instead of trying to make an AI rewrite the document, this project splits the problem into two clean responsibilities:

**AI does what AI is good at: classification**
The document text is extracted and sent to Google Gemini, which identifies sensitive expressions — names, addresses, phone numbers, emails, dates, locations. It returns a structured list. That is all it is asked to do.

**Specialised tools do what they are good at: precision editing**
The pipeline uses the exact word coordinates from the PDF's internal structure to locate each sensitive expression and permanently remove it — not cover it.

```
Input PDF
    │
    ├── Extract text + word coordinates (PyMuPDF)
    ├── Extract embedded images (PyMuPDF)
    │
    ├── AI classifies sensitive expressions (Google Gemini)
    ├── AI classifies sensitive words in images (Google Gemini)
    │
    ├── Step 1 ── False redact demo (black boxes over text, data still extractable)
    ├── Step 2 ── True text redact (text permanently removed via PDF redaction API)
    ├── Step 3 ── Image redact (sensitive regions located by EasyOCR, drawn black)
    └── Step 4 ── Metadata redact (author, creator, dates wiped)
    │
    └── Output: fully redacted PDF
```

### What the AI detected (live run output)

```
Page 1: March 21 2026 · Rome, Italy · Alexandria, VA · Elias Thorne
        Sarah Vance · +1-202-555-0198 · Marcus "The Ghost" Reed
        m.reed.secure@protonmail.ch · Count Alessandro Valerius
        142 Via della Lungaretta, Rome, IT · +39-06-555-4321
        MARCH 18-19, 2026 · BARNABY

Page 2: BARNABY · 88 Piazza Santa Maria · Alexandria Preservation Facility
        7210 Oakhaven Lane, VA · 1502 · +39-06-555-4321 · Sarah Vance

Images: Sarah Vance (handwritten) · Phase IV · Exfiltration · 03:05 AM
        Agent Thorne · BARNABY · Marcus Reed
```

All expressions: detected ✅. All expressions: redacted ✅.

---

## The false redact demonstration

One of the outputs (`1_false_redact.pdf`) deliberately shows the failure mode: black boxes drawn over the text, but the underlying data intact. Running a text extractor on that file returns all the sensitive information in full.

The next output (`2_redacted_text.pdf`) uses PyMuPDF's redaction API, which physically removes the text from the file structure before drawing the box. The same extractor returns nothing.

This is the difference between hiding data and removing it.

---

## Output files

| File | What it shows |
|---|---|
| `1_false_redact.pdf` | Black boxes drawn over text — data still extractable |
| `2_redacted_text.pdf` | True text redaction — underlying data permanently removed |
| `3_redacted_images.pdf` | Images also redacted using OCR-based localisation |
| `4_redacted_final.pdf` | Final output with metadata wiped |

---

## Known limitations

**Handwritten text in images**

EasyOCR is trained primarily on printed text. When the AI identifies a sensitive word as handwritten, the pipeline cannot locate it precisely — so it redacts most of the image as a conservative fallback. A production-grade solution for handwritten content would use a dedicated model such as Microsoft TrOCR.

**AI coordinate unreliability**

Both for text and images, early versions of this pipeline asked the AI to return the coordinates of sensitive content directly — word positions in the PDF, pixel bounding boxes in images.

In both cases this proved unreliable. AI models preprocess input internally before analysis: PDFs are rendered to images, images are resized or cropped. The coordinates returned do not correspond to the original document dimensions. Several approaches were tested to work around this — including overlaying a visible coordinate grid on images so the model could read positions directly from the content — none produced consistent results across different inputs.

The current approach separates the two concerns entirely: the AI classifies what is sensitive, PyMuPDF locates text using the PDF's internal word coordinates, and EasyOCR locates text inside images using optical character recognition. Each tool does only what it is good at.

For images, the localisation is not pixel-perfect. While EasyOCR can extract individual words, this approach proved unreliable in practice — small but critical character errors would occur. For example, a timestamp like “03:05 AM” could be incorrectly read as “03.05 AM”, which is enough to break downstream processing.

Extracting full lines of text produced much more accurate results, but introduced a different limitation: only the position of the entire line is known, not the exact position of each word within it.

To work around this, an approximate method is used. The system estimates where a word is likely to be within the line by assuming an average character width and calculating its relative position. Since this cannot be perfectly accurate — especially with narrow characters like “i” or “l” — an additional safety margin is applied, slightly expanding the bounding box on both sides to ensure the sensitive content is fully covered.

This is a known trade-off between accuracy and reliability. In a production-grade system, a more precise OCR model capable of word-level localisation would reduce the need for such approximations.

**PDF structure variation**

PDFs generated by different software can have unusual internal structures (non-standard character spacing, missing whitespace characters, split word objects). The word-matching algorithm handles punctuation differences and multi-line expressions, but edge cases in heavily non-standard PDFs may require tuning.

---

## Quick Start

```bash
git clone https://github.com/hasff/python-pdf-sensitive-data-redactor.git
cd python-pdf-sensitive-data-redactor
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Get a free Gemini API key at [Google AI Studio](https://aistudio.google.com) — no billing required for testing.

Create a `.env` file in the project root:
```
GEMINI_API_KEY=your_key_here
```

Place your PDF in `input/`, update the filename in `program.py`, then run:
```bash
python program.py
```

Output files will be generated in `output/`.

> **Note:** `DEBUG = True` in `program.py` uses mock AI responses without consuming API quota. Set `DEBUG = False` to run the full pipeline.

---

## Need PDF redaction for your documents?

I build automated document processing pipelines for:

- legal and compliance teams that need auditable redaction workflows
- companies handling GDPR, HIPAA or other data protection requirements
- HR and recruitment teams processing CVs and personal records
- any workflow that involves sharing documents externally with sensitive data removed

📩 Contact: hugoferro.business(at)gmail.com

🌐 Courses and professional tools: https://hasff.github.io/site/

---

## Further Learning

The PDF manipulation techniques used in this project — coordinate-based text extraction, image handling, redaction APIs — are covered in depth in my course:

[**Python PDF Handling: From Beginner to Winner (PyMuPDF)**](https://www.udemy.com/course/python-pdf-handling-from-beginner-to-winner/?referralCode=E7B71DCA8314B0BAC4BD)

The repository is fully usable on its own. The course provides the deeper understanding behind the decisions made here.
