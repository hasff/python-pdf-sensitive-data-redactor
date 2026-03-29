import os
import json
import io
import string

from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
from dotenv import load_dotenv
import easyocr
import fitz

from collections import defaultdict

from tools.draw_lines import draw_boxes, draw_boxes_in_doc



def simulate_gemini_response_text():
    json_str = '[["March 21, 2026", "Rome, Italy", "Alexandria, VA", "Elias Thorne", "Sarah Vance", "+1-202-555-0198", "Marcus \\"The Ghost\\" Reed", "m.reed.secure@protonmail.ch", "Count A. Valerius", "142 Via della Lungaretta, Rome, IT", "+39-06-555-4321", "MARCH 18-19, 2026", "BARNABY"], ["BARNABY", "88 Piazza Santa Maria", "Alexandria Preservation Facility", "7210 Oakhaven Lane, VA", "1502", "BARNABY", "+39-06-555-4321", "BARNABY", "Sarah Vance"]]'
    return json.loads(json_str)

def simulate_gemini_response_images():
    json_str = '{"113": [["Sarah Vance", true]], "115": [["Phase IV", false], ["Exfiltration", false], ["03:05 AM", false], ["Agent Thorne", false], ["BARNABY", false], ["Marcus Reed", false]]}'
    data = json.loads(json_str)
    data = {int(k): v for k, v in data.items()}
    return data

load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = "gemini-2.5-flash"
MODEL = "gemini-2.5-flash-lite"

BASE_DIR = Path(__file__).parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"

DEBUG = True  # Set to False to run the full pipeline with a real image and API call

def _words_match(w1, w2):
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

def extract_text(pdf_path):
    raw_text = []   # [page1_text, page2_text, ...]
    pages_words = []  # [[(x0, y0, x1, y1, word1, pno, lno, bno)], ...]
    pages_words_indexes = [] 
    
    doc = fitz.open(pdf_path)
    
    for page_num, page in enumerate(doc):
        raw_text.append(page.get_text())

        page_words = page.get_text("words")
        pages_words.append(page_words)

        pages_words_indexes.append({})

        for idx, word in enumerate(page_words):
            text = word[4]
            if text not in pages_words_indexes[-1]:
                pages_words_indexes[-1][text] = []
            pages_words_indexes[-1][text].append(idx)
    
    doc.close()
    return raw_text, pages_words, pages_words_indexes

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

def redact_text(pdf_path, output_path, pages_boxes):
    doc = fitz.open(pdf_path)
    
    for page_num, boxes in enumerate(pages_boxes):
        page = doc[page_num]
        
        for box in boxes:
            x0, y0, x1, y1 = box
            rect = fitz.Rect(x0, y0, x1, y1)

            page.add_redact_annot(rect, text="___", fontsize=8)
        
        page.apply_redactions()
    
    draw_boxes_in_doc(doc, pages_boxes, color= (0,0,0), fill= (0,0,0))


    doc.save(output_path)
    doc.close()
    

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
    
    _json = json.dumps(images_boxes)
    return images_boxes

def redact_images(pdf_path, output_path, images, imgs_words):
    
    ocr_reader = easyocr.Reader(['en'])

    doc = fitz.open(pdf_path)
    img_changed = False

    for image in images:
        img_xref = image['xref']
        if img_xref not in imgs_words:
            continue
            
        words = imgs_words[img_xref]
        if not words:
            continue
  
        ocr_results = ocr_reader.readtext(image["bytes"])

        pil_image = Image.open(io.BytesIO(image["bytes"]))
        draw = ImageDraw.Draw(pil_image)
        
        for sensitive_word, is_handwritten in words:
            if is_handwritten:
                print(f"⚠️  WARNING: '{sensitive_word}' is handwritten")
                padding = 25
                x0 = y0 = padding
                x1, y1 = (w - padding for w in pil_image.size)
                draw.rectangle([x0, y0, x1, y1], fill="black") 
                img_changed = True
            else:
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
                        
                        draw.rectangle([word_x0 - compensation, y0, word_x1 + compensation, y1], fill="black")                  
                        img_changed = True
                        word_detected = True
                        break
                print(f"{"✅" if word_detected else "❌"} { sensitive_word}")

        if img_changed:
            raw_bytes = pil_image.tobytes()
            doc.update_stream(img_xref, raw_bytes, new=0)  
        
    if img_changed:
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


def main():
    filePath = INPUT_DIR / "OPERATION_VERMILION_WHISKER.pdf"
    print(fitz.__version__)
    if not filePath.exists():
        print(f"Error: File {filePath} not found.")
        return

    raw_text, pages_words, pages_words_indexes = extract_text(filePath)

    if DEBUG:
        pages_sensitive_expressions = simulate_gemini_response_text() # Mock data for development
    else:
        formatted_text = ""
        for i, text in enumerate(raw_text):
            formatted_text += f"\n--- PAGE {i+1} ---\n{text}"        
        pages_sensitive_expressions = detect_sensitive_words_in_text(formatted_text) # Live API call

    # detect words in document.
    pages_boxes = []
    
    for page_no, page_sensitive_expressions in enumerate(pages_sensitive_expressions): 
        print(f"===========> page no {page_no + 1}")
        pages_boxes.append([])

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

            for page_word_index in all_indexes:                    
                candidate = page_words[page_word_index: page_word_index + expression_words_count]
                candidate_words = [w[4] for w in candidate]

                if all(_words_match(c, s) for c, s in zip(candidate_words, page_sensitive_expression_split)):
                    lines = defaultdict(list)
                    for word in candidate:
                        line_key = (word[5], word[6])  # block_no, line_no
                        lines[line_key].append(word)

                    for line_words in lines.values():
                        box = (line_words[0][0], line_words[0][1], line_words[-1][2], line_words[-1][3])
                        pages_boxes[-1].append(box)

                    print(f"✅ '{page_sensitive_expression}'")
                    # break
            else:
                print(f"❌ '{page_sensitive_expression}' não encontrado")            


    draw_boxes(filePath, OUTPUT_DIR / "false_redact.pdf", pages_boxes, color= (0,0,0), fill= (0,0,0))

    redacted_file_path = OUTPUT_DIR / "redacted.pdf"
    redact_text(filePath, redacted_file_path, pages_boxes)


    # images = extract_images(filePath)
    # if DEBUG:
    #     imgs_words = simulate_gemini_response_images()
    # else:
    #     imgs_words = detect_sensitive_words_in_images(images)    

    # redacted_file_path2 = OUTPUT_DIR / "redacted2.pdf"
    # # redact_images(filePath, redacted_file_path2, images, imgs_words)
    # redact_images(redacted_file_path, redacted_file_path2, images, imgs_words)

    a = 10


if __name__ == "__main__":
    main()
