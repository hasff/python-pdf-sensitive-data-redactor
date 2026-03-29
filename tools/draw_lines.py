import fitz  # pymupdf


def draw_boxes(input_pdf, output_pdf, pages_boxes, color=(1, 0, 0), fill= None):
    doc = fitz.open(input_pdf)

    for page_num, page in enumerate(doc):
        page_boxes = pages_boxes[page_num]

        for word in page_boxes:
            bbox = word[0:4]
            rect = fitz.Rect(bbox)

            page.draw_rect(
                rect,
                fill= fill,
                color=color,
                width=0.5
            )         

    doc.save(output_pdf)

def draw_boxes_in_doc(doc, pages_boxes, color=(1, 0, 0), fill= None):

    for page_num, page in enumerate(doc):
        page_boxes = pages_boxes[page_num]

        for word in page_boxes:
            bbox = word[0:4]
            rect = fitz.Rect(bbox)

            page.draw_rect(
                rect,
                fill= fill,
                color=color,
                width=0.5
            )
