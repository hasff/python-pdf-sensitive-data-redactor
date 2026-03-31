import os
import json
import io
import string

from google import genai
from google.genai import types
from PIL import Image, ImageDraw
from pathlib import Path
from dotenv import load_dotenv
import easyocr
import fitz
from collections import defaultdict

from tools.draw_lines import draw_boxes, draw_boxes_in_doc


# Do not display warning about pin memory (torch) and CUDA (easyocr)
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="torch")
import logging
logging.getLogger("easyocr").setLevel(logging.ERROR)


# ♦───────────────────────────────────────────────────────────────
#       CONFIGURATION
# ♦───────────────────────────────────────────────────────────────
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = "gemini-2.5-flash"
MODEL = "gemini-2.5-flash-lite"

BASE_DIR = Path(__file__).parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"

DEBUG = False  # Set to False to run the full pipeline with a real image and API call


# ♦───────────────────────────────────────────────────────────────
#       DEBUG MOCKS 🪲
# ♦───────────────────────────────────────────────────────────────
def _simulate_text_response():
    json_str = '[["March 21, 2026", "Rome, Italy", "Alexandria, VA", "Elias Thorne", "Sarah Vance", "+1-202-555-0198", "Marcus \\"The Ghost\\" Reed", "m.reed.secure@protonmail.ch", "Count A. Valerius", "142 Via della Lungaretta, Rome, IT", "+39-06-555-4321", "MARCH 18-19, 2026", "BARNABY"], ["BARNABY", "88 Piazza Santa Maria", "Alexandria Preservation Facility", "7210 Oakhaven Lane, VA", "1502", "BARNABY", "+39-06-555-4321", "BARNABY", "Sarah Vance"]]'
    return json.loads(json_str)

def _simulate_images_response():
    json_str = '{"113": [["Sarah Vance", true]], "115": [["Phase IV", false], ["Exfiltration", false], ["03:05 AM", false], ["Agent Thorne", false], ["BARNABY", false], ["Marcus Reed", false]]}'
    data = json.loads(json_str)
    data = {int(k): v for k, v in data.items()}
    return data


# ♦───────────────────────────────────────────────────────────────
#       DATA EXTRACTION 📄
# ♦───────────────────────────────────────────────────────────────
def extract_text(pdf_path):  
    pages_words = []  # [[(x0, y0, x1, y1, word1, pno, lno, bno)], ...]
    pages_words_indexes = [] 
    
    doc = fitz.open(pdf_path)
    formatted_text = ""

    for page_num, page in enumerate(doc):
        page_words = page.get_text("words")
        pages_words.append(page_words)

        pages_words_indexes.append({})

        for idx, word in enumerate(page_words):
            text = word[4]
            if text not in pages_words_indexes[-1]:
                pages_words_indexes[-1][text] = []
            pages_words_indexes[-1][text].append(idx)

        page_text = " ".join(w[4] for w in page_words)
        formatted_text += f"\n--- PAGE {page_num + 1} ---\n{page_text}"
    
    doc.close()
    return formatted_text, pages_words, pages_words_indexes

def extract_images(pdf_path):
    doc = fitz.open(pdf_path)
    doc_images = []

    for xref in range(1, doc.xref_length()):
        if doc.xref_is_image(xref):
            base_image = doc.extract_image(xref)
            for page in doc:
                rects = page.get_image_rects(xref)
                for rect in rects:
                    doc_images.append({
                        "xref": xref,
                        "bytes": base_image["image"],
                        "bbox": rect,
                        "ext": base_image["ext"],
                        "width": base_image["width"],
                        "height": base_image["height"]
                    })

    doc.close()
    return doc_images


# ♦───────────────────────────────────────────────────────────────
#       AI DETECTION 🔎
# ♦───────────────────────────────────────────────────────────────
def detect_sensitive_words_in_text(text):
    client = genai.Client(api_key=API_KEY, http_options={'api_version': 'v1'})
    
    query = """
    Analyse the text and detect sensitive information: names, addresses, phone numbers, emails, dates, locations.
    Return ONLY a raw JSON array of arrays. No markdown, no extra text, no code fences.
    Each inner array contains the sensitive expressions found on that page (preserve original casing and grouping).
    result[0] = page 1, result[1] = page 2, etc.
    If a page has no sensitive data, return an empty array for that page.
    Example:
    [
        ["John Smith", "Salt Lake City", "March 21 2026", "+1-555-0198"],
        ["Mary Thorn", "42 North Street"]
    ]
    """

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=[query, f"PDF CONTENT:\n{text}"],
            config=types.GenerateContentConfig(temperature=0)
        )
        return json.loads(response.text)
    except Exception as e:
        return f"Error: {e}"

def detect_sensitive_words_in_images(images):
    client = genai.Client(api_key=API_KEY, http_options={'api_version': 'v1'})
    
    images_boxes = {}
    
    for image in images:

        query = """    
        Analyse the image and detect sensitive information: names, addresses, phone numbers, emails, dates, locations.
        Return ONLY a raw JSON array. No markdown, no code fences, no backticks, no extra text whatsoever.
        Start your response with [ and end with ].
        Each element is: ["word", is_handwritten] where is_handwritten is true if the text is handwritten, false otherwise.
        Example: [["John Smith", false], ["42 North Street", false], ["Mary", true]]     
        """

        image_part = types.Part.from_bytes(
            data=image["bytes"],
            mime_type=f"image/{image['ext']}"
        )
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=[query, image_part],
                config=types.GenerateContentConfig(temperature=0)
            )
            boxes = json.loads(response.text)
            xref = image['xref']
            images_boxes[xref] = boxes

        except Exception as e:
            print(f"WARNING: erro ao processar imagem: {e}")
    
    return images_boxes


# ♦───────────────────────────────────────────────────────────────
#       HELPER FUNCTIONS 🛟
# ♦───────────────────────────────────────────────────────────────
def _words_match(w1, w2):
    """Returns True if two words differ only in punctuation."""
    remaining1 = w1
    remaining2 = w2
    for char in w2:
        remaining1 = remaining1.replace(char, '', 1)
    for char in w1:
        remaining2 = remaining2.replace(char, '', 1)
    
    return remaining1.strip(string.punctuation) == '' and remaining2.strip(string.punctuation) == ''

def _find_all_indexes(word, index):
    """Returns all indexes for keys that match the word, tolerating punctuation."""
    all_indexes = []
    for key in index:
        if word == key or _words_match(word, key):
            all_indexes.extend(index[key])
    return sorted(all_indexes)

# ♦───────────────────────────────────────────────────────────────
#       Bounding Boxes (BBOXES) FOR REDACTION 📦
# ♦───────────────────────────────────────────────────────────────
def map_sensitive_text_data_to_bboxes(sensitive_text_data, pages_words, pages_words_indexes):
    """
    Matches sensitive expressions to word bounding boxes in the PDF.
 
    sensitive_text_data - found sensitive expressions per page from the AI detection step
    pages_words - list of lists of words with their bounding boxes and positions, one list per page
    pages_words_indexes - list of dictionaries mapping words to their indexes in pages_words, one dictionary per page

    Exemple:
        sensitive_text_data = [
            ["John Smith", "March 21, 2026"], # page 1
            ["42 North Street"]               # page 2
        ]
        pages_words = [
            [(x0, y0, x1, y1, "John", block_no, line_no), (x0, y0, x1, y1, "Smith", block_no, line_no), (x0, y0, x1, y1, "John", block_no, line_no)],  # page 1
        ]
        pages_words_indexes = [
            {"John": [0, 2], "Smith": [1], ...},  # page 1, "John" appears in pages_words[0] and pages_words[2], "Smith" appears in pages_words[1]
        ]

    Returns:
        pages_boxes: list of lists of (x0, y0, x1, y1) tuples, one list per page
    """
    pages_bboxes = []
 
    print("\n📍 Mapping sensitive expressions to bounding boxes (text):")
    for page_no, page_sensitive_expressions in enumerate(sensitive_text_data): 
        print(f"──────────── Page {page_no + 1} ────────────")
        pages_bboxes.append([])

        page_words = pages_words[page_no]
        page_words_indexes = pages_words_indexes[page_no]

        for page_sensitive_expression in page_sensitive_expressions:
            page_sensitive_expression_split = page_sensitive_expression.split()
            expression_words_count = len(page_sensitive_expression_split)

            first_word = page_sensitive_expression_split[0]
            all_indexes = _find_all_indexes(first_word, page_words_indexes)
            if not all_indexes:
                print(f"  ⚠️  '{page_sensitive_expression}' — first word '{first_word}' not found")
                continue
            
            matched = False
            for page_word_index in all_indexes:                    
                candidate = page_words[page_word_index: page_word_index + expression_words_count]
                candidate_words = [w[4] for w in candidate]

                if all(_words_match(c, s) for c, s in zip(candidate_words, page_sensitive_expression_split)):
                    lines = defaultdict(list)
                    for word in candidate:
                        line_key = (word[5], word[6])  # block_no, line_no
                        lines[line_key].append(word)

                    for line_words in lines.values():
                        bbox = (line_words[0][0], line_words[0][1], line_words[-1][2], line_words[-1][3])
                        pages_bboxes[-1].append(bbox)

                    matched = True

            if not matched:
                print(f"  ❌  '{page_sensitive_expression}' not found")
            else:
                print(f"✅ '{page_sensitive_expression}' found")
 
    return pages_bboxes

def map_sensitive_image_data_to_bboxes(sensitive_image_data, images):
    """
    Maps sensitive words detected in images to bounding boxes.

    Returns:
        dict[xref] = List[Tuple[x0, y0, x1, y1]]
    """
    ocr_reader = easyocr.Reader(['en'])
    image_bboxes = {}

    print("\n📍 Mapping sensitive expressions to bounding boxes (images):")

    for image in images:
        img_xref = image['xref']

        if img_xref not in sensitive_image_data:
            continue

        words = sensitive_image_data[img_xref]
        if not words:
            continue

        ocr_results = ocr_reader.readtext(image["bytes"])
        pil_image = Image.open(io.BytesIO(image["bytes"]))

        boxes = []

        for sensitive_word, is_handwritten in words:
            if is_handwritten:
                print(f"⚠️  WARNING: '{sensitive_word}' is handwritten")

                padding = 25
                x0 = y0 = padding
                x1, y1 = (w - padding for w in pil_image.size)

                boxes.append((x0, y0, x1, y1))

                # - Since the whole image will be redacted, we can skip to the next one!
                # NOTE: The previous point is true because easyOCR models used in this example are not good at detecting handwritten text, 
                # so we won't be able to reliably find the position of the handwritten word. 
                break 

            word_detected = False
            _sword = sensitive_word.lower()

            for (bbox, text, confidence) in ocr_results:
                _text = text.lower()

                if _sword in _text:
                    x0 = min(p[0] for p in bbox)
                    y0 = min(p[1] for p in bbox)
                    x1 = max(p[0] for p in bbox)
                    y1 = max(p[1] for p in bbox)

                    line_width = x1 - x0
                    char_width = line_width / len(_text)
                    idx = _text.find(_sword)

                    word_x0 = x0 + idx * char_width
                    word_x1 = word_x0 + len(_sword) * char_width

                    compensation = 40

                    boxes.append((
                        word_x0 - compensation,
                        y0,
                        word_x1 + compensation,
                        y1
                    ))

                    word_detected = True
                    break

            print(f"{'✅' if word_detected else '❌'} {sensitive_word}")

        if boxes:
            image_bboxes[img_xref] = boxes

    return image_bboxes

# ♦───────────────────────────────────────────────────────────────
#       REDACTION 📝
# ♦───────────────────────────────────────────────────────────────
def redact_text(pdf_path, output_path, pages_bboxes):
    doc = fitz.open(pdf_path)
    
    for page_num, bboxes in enumerate(pages_bboxes):
        page = doc[page_num]
        
        for bbox in bboxes:
            x0, y0, x1, y1 = bbox
            rect = fitz.Rect(x0, y0, x1, y1)

            page.add_redact_annot(rect, text="___", fontsize=8)
        
        page.apply_redactions()
    
    draw_boxes_in_doc(doc, pages_bboxes, color= (0,0,0), fill= (0,0,0))

    doc.save(output_path)
    doc.close()
    
def redact_images(pdf_path, output_path, images, images_bboxes):
    doc = fitz.open(pdf_path)

    for image in images: 

        xref = image["xref"]
        if xref not in images_bboxes:
            continue

        pil_image = Image.open(io.BytesIO(image["bytes"]))
        draw = ImageDraw.Draw(pil_image)       

        for bbox in images_bboxes[xref]:
            draw.rectangle(bbox, fill="black") 
        
        raw_bytes = pil_image.tobytes()
        doc.update_stream(xref, raw_bytes, new=0)

    doc.save(output_path)
    doc.close()            

def redact_metadata(pdf_path, output_path):
    doc = fitz.open(pdf_path)
    doc.set_metadata({
        "author": "",
        "producer": "",
        "creator": "",
        "title": "",
        "subject": "",
        "keywords": "",
        "creationDate": "",
        "modDate": ""
    })
    doc.save(output_path)
    doc.close()


# ♦───────────────────────────────────────────────────────────────
#       MAIN FUNCTION 🚀
# ♦───────────────────────────────────────────────────────────────
def main():
    filePath = INPUT_DIR / "OPERATION_VERMILION_WHISKER.pdf"
    print(fitz.__version__)
    if not filePath.exists():
        print(f"Error: File {filePath} not found.")
        return

    # DATA EXTRACTION 📄
    formatted_text, pages_words, pages_words_indexes = extract_text(filePath)
    images = extract_images(filePath)

    # AI DETECTION 🔎
    if DEBUG:
        sensitive_text_data = _simulate_text_response() # Mock data for development
        sensitive_image_data = _simulate_images_response()
    else:       
        sensitive_text_data = detect_sensitive_words_in_text(formatted_text) # Live API call
        sensitive_image_data = detect_sensitive_words_in_images(images)

    print("\n📍 Detected by AI sensitive expressions in text:")
    for page_no, page in enumerate(sensitive_text_data, start=1):
        print(f"Page {page_no}: {page}")

    print("\n📍 Detected by AI sensitive expressions in images:")
    for img_xref, words in sensitive_image_data.items():
        print(f"Image xref {img_xref}: {words}")

    print()

    # Bounding Boxes (BBOXES) FOR REDACTION 📦
    redaction_bboxes_per_page = map_sensitive_text_data_to_bboxes(sensitive_text_data, pages_words, pages_words_indexes)
    redaction_bboxes_per_image = map_sensitive_image_data_to_bboxes(sensitive_image_data, images)
    

    # REDACTION 📝
    # ── Step 1: False redact (visual only — text still extractable)
    _source_file = filePath
    _result_file = OUTPUT_DIR / "1_false_redact.pdf"    
    draw_boxes(_source_file, _result_file, redaction_bboxes_per_page, color= (0,0,0), fill= (0,0,0))


    # ── Step 2: True text redact
    _source_file = filePath
    _result_file = OUTPUT_DIR / "2_redacted_text.pdf"
    redact_text(_source_file, _result_file, redaction_bboxes_per_page)

    # ── Step 3: Image redact
    _source_file = _result_file
    _result_file = OUTPUT_DIR / "3_redacted_images.pdf"   
    redact_images(_source_file, _result_file, images, redaction_bboxes_per_image)
    
    # ── Step 4: Metadata redact
    _source_file = _result_file
    _result_file = OUTPUT_DIR / "4_redacted_metadata.pdf"    
    redact_metadata(_source_file, _result_file)



if __name__ == "__main__":
    main()
