"""Compare individual parts in a printed step panel with local Rebrickable images."""
import math
import hashlib
import re
from collections import defaultdict

import cv2
import numpy as np
import pymupdf as pdf
from PIL import Image
from lego_pdf_text import index_part_labels, quantity_words


def foreground(rgb, background):
    delta = np.abs(rgb.astype(np.int16) - background)
    return np.uint8(delta.max(axis=2) > 17)


def flat_face(gray, mask):
    """Share of the silhouette occupied by one uninterrupted parallelogram."""
    values = gray[mask != 0]
    histogram = np.bincount(values, minlength=256)
    peak = np.convolve(histogram, np.ones(9), mode="same").argmax()
    region = ((np.abs(gray.astype(int)-peak) <= 6) & (mask != 0)).astype(np.uint8)
    contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return 0.
    face = max(contours, key=cv2.contourArea)
    corners = cv2.approxPolyDP(face, .035*cv2.arcLength(face, True), True)
    if len(corners) != 4 or not cv2.isContourConvex(corners):
        return 0.
    pts = corners[:,0].astype(float)
    if np.linalg.norm(pts[0]+pts[2]-pts[1]-pts[3]) > .15*np.linalg.norm(pts.max(0)-pts.min(0)):
        return 0.
    area = cv2.drawContours(np.zeros_like(region), [face], -1, 1, -1)
    return np.count_nonzero(area*region)/len(values)


def descriptor(rgb, mask):
    x, y, w, h = cv2.boundingRect(mask)
    if min(w, h) < 3:
        return None
    mask = mask[y:y+h, x:x+w]
    rgb = rgb[y:y+h, x:x+w]
    pixels = rgb[mask != 0]
    color = np.percentile(pixels, 65, axis=0).astype(np.uint8)
    lab = cv2.cvtColor(color.reshape(1, 1, 3), cv2.COLOR_RGB2LAB).reshape(3).astype(float)
    # Match the part's internal relief across changes in print contrast.
    luminance = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    flat = flat_face(luminance, mask)
    gray = luminance.astype(np.float32)
    gray -= gray[mask != 0].mean()
    gray /= max(12, gray[mask != 0].std())
    gray[mask == 0] = 0
    def resize(arr):
        return cv2.resize(arr.astype(np.float32), (40, 40), interpolation=cv2.INTER_AREA)
    # The spacing of repeated studs or pin holes distinguishes similar long parts.
    # Remove perspective slope before measuring horizontal repetition.
    cols = np.flatnonzero(mask.any(axis=0))
    top = np.argmax(mask[:, cols], axis=0)
    slope = np.polyfit(cols, top, 1)[0] if len(cols) >= 8 else 0
    deskew = np.float32([[1, 0, 0], [-slope, 1, max(0, slope*w)]])
    texture = cv2.warpAffine(gray, deskew, (w, h+int(abs(slope)*w)+2))
    texture = cv2.resize(texture, (128, 48), interpolation=cv2.INTER_AREA)
    power = np.abs(np.fft.rfft(texture * np.hanning(128), axis=1))**2
    spectrum = np.sqrt(power.sum(axis=0)[2:25])
    spectrum /= max(1e-6, np.linalg.norm(spectrum))
    return (math.log(w/h), lab, resize(mask), resize(np.clip(gray, -2, 2)), spectrum, flat)


def reference_image(path):
    with Image.open(path) as im:
        rgba = im.convert("RGBA")
        rgba.thumbnail((256, 256))
        white = Image.new("RGB", rgba.size, "white")
        white.paste(rgba, mask=rgba.getchannel("A"))
        rgb = np.array(white)
    mask = foreground(rgb, [255, 255, 255])
    # Fill the white faces enclosed by the outline of a white brick.
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    cv2.drawContours(mask := np.zeros_like(mask), [max(contours, key=cv2.contourArea)], -1, 1, -1)
    return descriptor(rgb, mask)


def printed_part_codes(page, rect=None):
    circles = []
    for drawing in page.get_cdrawings():
        box = pdf.Rect(drawing["rect"])
        if (9 <= box.width <= 16 and abs(box.height - box.width) < .3
                and any(item[0] == "c" for item in drawing["items"])
                and (rect is None or box.intersects(rect))):
            circles.append(box)
    codes = set()
    for x, y, right, bottom, text, *_ in page.get_text("words", clip=rect):
        if re.fullmatch(r"[1-9]\d?", text):
            center = ((x + right) / 2, (y + bottom) / 2)
            if any(abs(center[0] - (b.x0 + b.x1) / 2) < 2
                   and abs(center[1] - (b.y0 + b.y1) / 2) < 2 for b in circles):
                codes.add((*center, text))
    return codes


def panel_parts(page, box, previews=None, signatures=None, codes=None):
    rect = pdf.Rect(box)
    markers = printed_part_codes(page, rect) if codes is not None else ()
    marker_images = [pdf.Rect(info["bbox"]) for info in page.get_image_info()
                     if min(pdf.Rect(info["bbox"]).width, pdf.Rect(info["bbox"]).height) > 4
                     and rect.contains(pdf.Rect(info["bbox"]))] if markers else ()
    images = []
    if signatures is not None:
        images = [(pdf.Rect(b["bbox"]), b) for b in page.get_text("dict", clip=rect + (-1,-1,1,1))["blocks"]
                  if b["type"] == 1]
    quantities = quantity_words(page, clip=rect)
    pix = page.get_pixmap(matrix=pdf.Matrix(3, 3), clip=rect, colorspace=pdf.csRGB, alpha=False)
    rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)
    bg = np.median(rgb[::4, ::4].reshape(-1, 3), axis=0)
    mask = foreground(rgb, bg)
    # Some revised manuals paint the current panel over an older one. The
    # covered labels still exist in the text layer but have no visible ink.
    labels = {}
    visible_quantities = []
    for quantity in quantities:
        x, y, right, bottom, word = quantity
        ink = mask[max(0, round(y*3-pix.y)+4):round(bottom*3-pix.y),
                   max(0, round(x*3-pix.x)):round(right*3-pix.x)]
        if np.count_nonzero(ink) >= 6:
            labels[round(x), round(y)] = (x, y, int(word[:-1]))
            visible_quantities.append(quantity)
    # Erase the panel border and printed quantities before separating touching boxes.
    mask[:5] = mask[-5:] = 0
    mask[:, :5] = mask[:, -5:] = 0
    for x, y, right, bottom, _ in visible_quantities:
        mask[max(0,round(y*3-pix.y)+4):round(bottom*3-pix.y),
             max(0,round(x*3-pix.x)-1):round(right*3-pix.x)+2] = 0
    n, components, stats, _ = cv2.connectedComponentsWithStats(mask)
    anchors = sorted(labels.values(), key=lambda a: (round(a[1]/3), a[0]))
    choices = []
    for j,(ax, ay, _) in enumerate(anchors):
        ax, ay = ax*3-pix.x, ay*3-pix.y
        for i in range(1,n):
            x,y,w,h,area = map(int, stats[i])
            dx,dy = abs(ax-x), abs(ay-y-h)
            if area >= 25 and dx < 28 and dy < 26:
                choices.append((dx+dy, j, i))
    selected, used = {}, set()
    for _, j, i in sorted(choices):
        if j not in selected and i not in used:
            selected[j] = i
            used.add(i)
    # A wide, angled plate may have its quantity beside the lower sloping edge.
    # Check the actual contour above the label after resolving left-aligned labels.
    alternatives = defaultdict(list)
    for j,(ax, ay, qty) in enumerate(anchors):
        if j in selected:
            continue
        left = round(ax*3-pix.x)
        baseline = ay*3-pix.y
        for i in range(1,n):
            x,y,w,h,area = map(int,stats[i])
            if i in used or area < 25 or not x <= left < x+w or not y < baseline:
                continue
            strip = components[y:y+h, left:min(x+w,left+12*(len(str(qty))+1))]
            occupied = np.flatnonzero((strip == i).any(axis=1))
            if occupied.size and -5 <= baseline-(y+occupied[-1]) <= 30:
                alternatives[j].append(i)
    for j, candidates in alternatives.items():
        if len(candidates) == 1 and sum(candidates[0] in opts for opts in alternatives.values()) == 1:
            selected[j] = candidates[0]
    if len(anchors) == 1 and not selected:
        ax, ay, _ = anchors[0]
        # A large diamond-shaped plate can extend below its quantity label.
        large = [i for i, (x,y,w,h,area) in enumerate(stats[1:], 1)
                 if area > mask.size*.12 and abs(ax*3-pix.x-x) < 12
                 and y < ay*3-pix.y < y+h+12]
        if len(large) == 1:
            selected[0] = large[0]
    fallback_boxes = {}
    if len(selected) != len(anchors):
        for info in page.get_image_info():
            artwork = pdf.Rect(info["bbox"])
            if not rect.contains(artwork) or min(artwork.width, artwork.height) < 4:
                continue
            candidates = sorted((abs(ax-artwork.x0) + abs(ay-artwork.y1), j)
                                for j, (ax, ay, _) in enumerate(anchors)
                                if (-2 <= ax-artwork.x0 <= 18 and abs(ay-artwork.y1) < 3)
                                or (abs(ax-artwork.x0) <= 2 and 3 <= artwork.y1-ay <= 16
                                    and artwork.y0 < ay-5))
            if candidates and (len(candidates) == 1 or candidates[1][0] - candidates[0][0] > 2):
                j = candidates[0][1]
                if j not in selected:
                    fallback_boxes.setdefault(j, set()).add(tuple(artwork))
    result = []
    for j,(_, _, qty) in enumerate(anchors):
        features = None
        preview = None
        signature = None
        code = None
        if j in selected:
            i = selected[j]
            x,y,w,h,_ = stats[i]
            part_mask = np.uint8(components[y:y+h,x:x+w] == i)
            contours, _ = cv2.findContours(part_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(part_mask, contours, -1, 1, -1)
            visual = descriptor(rgb[y:y+h,x:x+w], part_mask)
            if visual is not None:
                features = (visual, w/3)
                if markers:
                    marker_crop = pdf.Rect((x+pix.x)/3, (y+pix.y)/3,
                                           (x+w+pix.x)/3, (y+h+pix.y)/3) + (-1,-1,1,1)
                    found = [value for mx, my, value in markers if (mx, my) in marker_crop]
                    if len(found) == 1:
                        code = found[0]
                        artwork = max(marker_images, key=lambda b: (b & marker_crop).get_area(), default=None)
                        if artwork and (artwork & marker_crop).get_area() > .75 * artwork.get_area():
                            # Use the part illustration without the number and its leader.
                            left, top, right, bottom = (artwork * pdf.Matrix(3, 3)).irect
                            patch = rgb[top-pix.y:bottom-pix.y, left-pix.x:right-pix.x]
                            isolated = foreground(patch, bg)
                            if isolated.any():
                                clean = descriptor(patch, isolated)
                                if clean is not None:
                                    features = (clean, artwork.width)
                if previews is not None:
                    preview = Image.fromarray(np.where(part_mask[:,:,None], rgb[y:y+h,x:x+w], 235).astype(np.uint8))
                if signatures is not None:
                    crop = pdf.Rect((pix.x+x)/3, (pix.y+y)/3, (pix.x+x+w)/3, (pix.y+y+h)/3)
                    layers = [(b, asset) for b, asset in images if (b & crop).get_area() > 1]
                    digest = hashlib.sha256()
                    if len(layers) == 1 and max(abs(a-b) for a,b in zip(crop,layers[0][0])) < 2:
                        asset = layers[0][1]
                        digest.update(asset["image"])
                        digest.update(asset.get("mask") or b"")
                        digest.update(str(tuple(round(v,1) for v in asset["transform"][:4])).encode())
                    else:
                        digest.update(str((w,h)).encode())
                        digest.update(np.where(part_mask[:,:,None],rgb[y:y+h,x:x+w],235).astype(np.uint8).tobytes())
                    signature = digest.hexdigest()
        elif len(fallback_boxes.get(j, ())) == 1:
            artwork = pdf.Rect(next(iter(fallback_boxes[j])))
            left, top, right, bottom = (artwork * pdf.Matrix(3, 3)).irect
            patch = rgb[top-pix.y:bottom-pix.y, left-pix.x:right-pix.x]
            isolated = foreground(patch, bg)
            if isolated.any():
                visual = descriptor(patch, isolated)
                if visual is not None:
                    features = (visual, artwork.width)
                    if previews is not None:
                        preview = Image.fromarray(patch)
                    if signatures is not None:
                        digest = hashlib.sha256()
                        digest.update(str((patch.shape[1], patch.shape[0])).encode())
                        digest.update(np.where(isolated[:, :, None], patch, 235).astype(np.uint8).tobytes())
                        signature = digest.hexdigest()
        if signatures is not None:
            signatures.append(signature)
        if codes is not None:
            codes.append(code)
        if previews is not None:
            previews.append(preview)
        result.append((qty, features))
    return result


class PartMatcher:
    def __init__(self, candidates, *, index_first=False, reviewed=None, printed_geometry=False):
        self.index_first = index_first
        self.printed_geometry = printed_geometry
        self.reviewed = reviewed or {}
        self.inventory = {record[0]: (record, total) for record, _, total in candidates}
        self.catalog = self.inventory
        self.refs = []
        self.images = {}
        self.designs = {}
        self.index_features = {}
        self.index_colors = {}
        self.index_widths = {}
        self.index_codes = {}
        samples = defaultdict(dict)
        for record, path, total in candidates:
            self.designs[record[0]] = record[1]
            if path is None:
                continue
            try:
                features = reference_image(path)
            except (OSError, ValueError):
                continue
            if features is not None:
                self.refs.append((record, features, total))
                self.images[record[0]] = path
                samples[record[3]][path] = features[1]
        # Several photos of the same catalog color give a more stable reference
        # for glossy parts whose highlights appear much lighter in one photo.
        self.color_centers = {
            color: np.median(list(images.values()), axis=0)
            for color, images in samples.items() if color and len(images) >= 4
        }

    def load_index_checks(self, path):
        """Build an independent check from illustrations explicitly labelled in the PDF."""
        if not path or not self.inventory:
            return
        refs = {}
        with pdf.open(path) as doc:
            for page in reversed(doc):
                labels = [(number, x, y) for number, _, x, y in index_part_labels(page)
                          if number in self.inventory]
                if not labels:
                    if refs:
                        break
                    continue
                images = [pdf.Rect(s["bbox"]) for s in page.get_image_info()]
                markers = printed_part_codes(page) if self.printed_geometry else ()
                for number, ax, ay in labels:
                    boxes = {tuple(b) for b in images if abs(b.x0-ax)<2 and abs(b.y1-ay)<2
                             and min(b.width,b.height)>3}
                    if not boxes and self.printed_geometry:
                        # Some older LEGO PDFs leave a little over two points
                        # between the image and its printed quantity.
                        boxes = {tuple(b) for b in images if abs(b.x0-ax)<2 and abs(b.y1-ay)<3
                                 and min(b.width,b.height)>3}
                    if len(boxes) != 1:
                        continue
                    box = pdf.Rect(boxes.pop()) + (-1,-1,1,1)
                    codes = [code for mx, my, code in markers if box.x0 - 9 <= mx <= box.x1 + 9
                             and 0 <= my - box.y0 <= 12]
                    if len(codes) == 1:
                        self.index_codes[number] = codes[0]
                    pix = page.get_pixmap(matrix=pdf.Matrix(3,3),clip=box, colorspace=pdf.csRGB)
                    rgb = np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3)
                    bg = np.median(np.concatenate((rgb[0],rgb[-1],rgb[:,0],rgb[:,-1])),axis=0)
                    mask = foreground(rgb,bg)
                    contours,_ = cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
                    if contours:
                        mask.fill(0)
                        cv2.drawContours(mask,[max(contours,key=cv2.contourArea)],-1,1,-1)
                        f = descriptor(rgb,mask)
                        if f is not None:
                            refs[number] = f
                            self.index_widths[number] = box.width - 2
        if len(refs) >= 2:
            self.index_features = {k:np.stack([f[k] for f in refs.values()]) for k in (0,1,2,3)}
            self.index_ids = list(refs)
            if self.index_first or self.printed_geometry:
                self.templates = [self.print_template(f[2], f[3]) for f in refs.values()]
            self.index_colors = {number: features[1] for number, features in refs.items()}

    def printed_color_variant(self, color, design, ranked):
        """Resolve variants of an already recognized mold in the same print palette."""
        if np.linalg.norm(color[1:]-128) < 18:
            return None
        variants = []
        for _, record in ranked:
            sample = self.index_colors.get(record[0])
            if record[1] == design and sample is not None:
                variants.append((np.linalg.norm(color-sample), record))
        variants.sort(key=lambda pair: pair[0])
        if len(variants) < 2:
            return None
        distance, best = variants[0]
        next_distance, other = variants[1]
        if (best[3] != other[3] and distance <= 12 and next_distance-distance >= 4
                and distance <= .75*next_distance):
            return best
        return None

    def other_variant_supports(self, visual, record, design_margin=0.):
        """Compare the outline and relief independently of color."""
        if not record[1]:
            return False
        aspect, _, mask, relief, _, _ = visual
        refs = self.index_features
        distances = (.22*np.abs(refs[0]-aspect)
                     + .8*np.abs(refs[2]-mask).mean(axis=(1,2))
                     + .22*np.abs(refs[3]-relief).mean(axis=(1,2)))
        variant = float("inf")
        other = float("inf")
        for number, distance in zip(self.index_ids, distances):
            if number == record[0]:
                continue
            if self.designs.get(number) == record[1]:
                variant = min(variant, distance)
            else:
                other = min(other, distance)
        return variant < .14 and other - variant > design_margin

    def index_candidate(self, visual):
        if not self.index_features:
            return None
        aspect, color, mask, relief, _, _ = visual
        refs = self.index_features
        proportions = np.abs(refs[0]-aspect)
        colors = np.linalg.norm(refs[1]-color,axis=1)
        outlines = np.abs(refs[2]-mask).mean(axis=(1,2))
        surfaces = np.abs(refs[3]-relief).mean(axis=(1,2))
        distances = .22*proportions + .011*colors + .8*outlines + .22*surfaces
        best, second = np.argsort(distances)[:2]
        if distances[best] < .22 and distances[second]-distances[best] > .055:
            margin = distances[second]-distances[best]
            close_render = (distances[best] < .09 and margin > .065
                            and proportions[best] < .05 and colors[best] < 4
                            and outlines[best] < .04 and surfaces[best] < .23)
            if not close_render and distances[best] < .13 and colors[best] < 5:
                # A smooth bar and a friction pin can share a silhouette. Compare
                # the positions of ridges and highlights inside the overlapping faces.
                scores = {}
                for i in np.flatnonzero((proportions < .25) & (outlines < .13)):
                    interior = (mask > .7) & (refs[2][i] > .7)
                    if interior.sum() < 60:
                        continue
                    a, b = relief[interior], refs[3][i][interior]
                    if min(a.std(), b.std()) > .15:
                        scores[i] = np.corrcoef(a, b)[0,1]
                correlation = scores.pop(best, 0.)
                if scores and correlation > .80 and correlation - max(scores.values()) > .15:
                    close_render = True
            return self.index_ids[best], distances[best], margin, close_render
        return None

    def primary_index_match(self, visual, index, needed):
        """Accept a distinct printed candidate only when its internal detail agrees."""
        number, distance, margin, _ = index
        entry = self.catalog.get(number)
        if not entry or entry[1] < needed or distance > .15 or margin < .075:
            return None
        i = self.index_ids.index(number)
        aspect, color, mask, relief, _, _ = visual
        refs = self.index_features
        if (abs(aspect-refs[0][i]) > .06 or np.linalg.norm(color-refs[1][i]) > 7
                or np.abs(mask-refs[2][i]).mean() > .065):
            return None
        # A little smoothing suppresses print pixels while retaining studs,
        # holes and edges. Check only the shared interior of the two parts.
        interior = (mask > .8) & (refs[2][i] > .8)
        if interior.sum() < 100:
            return None
        a = cv2.GaussianBlur(relief, (3,3), .8)[interior]
        b = cv2.GaussianBlur(refs[3][i], (3,3), .8)[interior]
        if min(a.std(), b.std()) < .15 or np.corrcoef(a,b)[0,1] < .88:
            return None
        return entry[0], "Match via PDF-index", round(100*math.exp(-2*distance))

    @staticmethod
    def print_template(mask, relief):
        return np.dstack((mask, cv2.GaussianBlur(relief, (0,0), 1.5)))

    def match_printed_geometry(self, visual, width, needed, code=None):
        """Compare the photographed outline and mould details to the manual's drawings."""
        refs = self.index_features
        if not self.printed_geometry or not refs or len(self.index_ids) < .75 * len(self.inventory):
            return None
        aspect, color, mask, relief, _, _ = visual
        proportions = np.abs(refs[0] - aspect)
        colors = np.linalg.norm(refs[1] - color, axis=1)
        outlines = np.abs(refs[2] - mask).mean(axis=(1, 2))
        sample = self.print_template(mask, relief)[3:-3, 3:-3]
        scores = []
        coded = [i for i, n in enumerate(self.index_ids) if code and self.index_codes.get(n) == code]
        eligible = (coded if coded else np.flatnonzero((proportions < .32) & (colors < 50) & (outlines < .26)))
        for i in eligible:
            number = self.index_ids[i]
            entry = self.inventory.get(number)
            if not entry or entry[1] < needed:
                continue
            similarity = float(cv2.matchTemplate(self.templates[i], sample, cv2.TM_CCOEFF_NORMED).max())
            if similarity < (.35 if coded else .76) or (coded and colors[i] > 30):
                continue
            # The width penalty is soft because manuals sometimes zoom in on small parts.
            width_gap = abs(math.log(width / (1.3 * self.index_widths[number])))
            if coded and width_gap > .55:
                continue
            cost = 1 - similarity + .2 * proportions[i] + .007 * colors[i] + .2 * outlines[i] + .15 * width_gap
            scores.append((cost, number, similarity))
        if not scores:
            return self.match_printed_geometry(visual, width, needed) if coded else None
        scores.sort()
        cost, number, similarity = scores[0]
        margin = scores[1][0] - cost if len(scores) > 1 else 1
        status = "Match via PDF-index" if (coded or similarity >= .89) and margin >= .05 else "Controleren via PDF-index"
        return self.inventory[number][0], status, round(100 * math.exp(-2 * cost))

    def aligned_index_match(self, visual, index, needed, catalog_choice=None):
        # Small index illustrations can place a stud a couple of pixels away
        # from its position in the larger step drawing after normalization.
        aspect, color, mask, relief, _, _ = visual
        sample = self.print_template(mask, relief)[3:-3,3:-3]
        refs = self.index_features
        candidates = np.flatnonzero((np.abs(refs[0]-aspect) < .24) &
                                    (np.linalg.norm(refs[1]-color,axis=1) < 24))
        scores = []
        for i in candidates:
            score = cv2.matchTemplate(self.templates[i], sample, cv2.TM_CCOEFF_NORMED).max()
            scores.append((float(score), self.index_ids[i]))
        if not scores:
            return None
        scores.sort(reverse=True)
        score, number = scores[0]
        entry = self.catalog.get(number)
        if not entry or needed > entry[1]:
            return None
        distinct = len(scores) == 1 or score-scores[1][0] >= .045
        if distinct and ((number == index[0] and score >= .88)
                         or (number == catalog_choice and score >= .96)):
            return entry[0], "Match via PDF-index", round(100*score)
        # On a long vertical shaft a one-pixel shift changes much of the
        # normalized width. Compare the height of the stop ring separately.
        if aspect < -1.5 and number == index[0] and index[2] > .20 and score > .70:
            profile = mask.mean(axis=1)[2:-2]
            ref = refs[2][self.index_ids.index(number)].mean(axis=1)[2:-2]
            ring = profile > (np.median(profile)+profile.max())/2
            ref_ring = ref > (np.median(ref)+ref.max())/2
            if (min(profile.max()-np.median(profile), ref.max()-np.median(ref)) > .15
                    and 2 <= ring.sum() <= len(ring)//4
                    and np.count_nonzero(ring & ref_ring) >= .8*np.count_nonzero(ring | ref_ring)):
                return entry[0], "Match via PDF-index", round(100*score)
        return None

    def refine_panel(self, parts, matches, repeat):
        """Check doubtful rows against the drawing scale of other parts in this panel."""
        if not self.index_first or not self.index_features:
            return
        scales = [query[1]/self.index_widths[rec[0]]
                  for (_,query),(rec,status,_) in zip(parts,matches)
                  if query and rec and status in ("Match via PDF-index", "Ook in PDF-index")
                  and rec[0] in self.index_widths]
        if not scales:
            return
        scale = float(np.median(scales))
        if max(abs(s/scale-1) for s in scales) > .12:
            return
        refs = self.index_features
        for j, ((qty,query), (_,status,_)) in enumerate(zip(parts,matches)):
            if status != "Controleren" or not query:
                continue
            visual, width = query
            aspect, color, mask, relief, _, _ = visual
            template = self.print_template(mask,relief)[3:-3,3:-3]
            scores = []
            for i, number in enumerate(self.index_ids):
                size_gap = abs(math.log(width / (scale*self.index_widths[number])))
                color_gap = np.linalg.norm(color-refs[1][i])
                if size_gap > .22 or color_gap > 25 or abs(aspect-refs[0][i]) > .25:
                    continue
                corr = float(cv2.matchTemplate(self.templates[i], template, cv2.TM_CCOEFF_NORMED).max())
                scores.append((corr - .6*size_gap - .005*color_gap, number, corr, size_gap, color_gap))
            if not scores:
                continue
            scores.sort(reverse=True)
            score, number, corr, size_gap, color_gap = scores[0]
            entry = self.catalog.get(number)
            if (entry and entry[1] >= qty*repeat and size_gap < .08 and color_gap <= 15
                    and corr >= .90 and (len(scores)==1 or score-scores[1][0] > .075)):
                matches[j] = entry[0], "Match via PDF-index", round(100*score)

    def match(self, query, needed, signature=None, code=None):
        learned = self.inventory.get(self.reviewed.get(signature))
        if learned and learned[1] >= needed:
            return learned[0], "Eerder gecontroleerd", 100
        if query is None:
            return None, "Onbekend", 0
        visual, width = query
        printed = self.match_printed_geometry(visual, width, needed, code)
        if printed:
            return printed
        index = self.index_candidate(visual)
        if self.index_first and index:
            printed = self.primary_index_match(visual, index, needed)
            if printed is None:
                printed = self.aligned_index_match(visual, index, needed)
            if printed:
                return printed
        aspect, color, mask, relief, pattern, flat = visual
        ranked = []
        for record, (ratio, lab, silhouette, surface, ref_pattern, ref_flat), total in self.refs:
            if total < needed:
                continue
            cd = color-lab
            color_gap = np.linalg.norm(cd * [.45, 1, 1])
            center = self.color_centers.get(record[3])
            if center is not None:
                typical_gap = np.linalg.norm((color-center) * [.75, 1, 1])
                color_gap = .4*color_gap + .6*typical_gap
            shape = min(.7*np.abs(mask-m).mean() + .20*np.abs(relief-v).mean()
                        for m,v in ((silhouette,surface),(silhouette[:,::-1],surface[:,::-1])))
            distance = (shape + .17*abs(aspect-ratio) + .008*color_gap
                        + .42*(1-np.dot(pattern, ref_pattern)))
            if flat > .35:
                # Catalog photos can have a highlight across a smooth tile face.
                # The plain rectangular tile description supplies a second cue.
                plain_tile = re.fullmatch(r"Tile \d+ x \d+(?: with Groove)?", record[2])
                if not plain_tile:
                    distance += .80 * max(0., flat-ref_flat)
            # Catalog dimensions give a soft scale cue. Allow either orientation
            # and a margin for manuals drawn at a different scale.
            dims = re.match(r"(?:Plate|Brick|Tile)(?: Special| Round)? (\d+) x (\d+)\b", record[2])
            if dims:
                a, b = sorted(map(int, dims.groups()))
                low, high = 8.5*a+5.5*b, 5.5*a+8.5*b
                size_gap = max(0, math.log(low*.84/width), math.log(width/(high*1.17)))
                distance += min(.65, .47*size_gap)
            ranked.append((distance, record))
        ranked.sort(key=lambda r:r[0])
        if not ranked:
            return None, "Onbekend", 0
        distance, record = ranked[0]
        if self.index_first and index and index[0] != record[0] and distance <= .52:
            agreement = self.aligned_index_match(visual, index, needed, record[0])
            if agreement:
                return agreement
        if distance <= .52 and (not index or self.designs.get(index[0]) == record[1]):
            variant = self.printed_color_variant(color, record[1], ranked)
            if variant and variant != record and (not index or index[0] == variant[0]):
                cost = next(cost for cost, candidate in ranked if candidate == variant)
                return variant, "Kleur via PDF-index", round(100*math.exp(-2*cost))
        # PDF drawings of the same element can be almost identical even when
        # the catalog photo shows the other side or has different lighting.
        if index and (index[0] != record[0] or distance > .52):
            candidate = next((rec for _, rec in ranked if rec[0] == index[0]), None)
            if candidate is not None:
                # In a different color, the same mold must still beat competing
                # shapes without using any color information.
                if index[3] or (index[1] < .18 and index[2] > .08 and
                                self.other_variant_supports(visual, candidate, design_margin=.045)):
                    return candidate, "Match via PDF-index", round(100*math.exp(-2*index[1]))
        if distance > .52:
            # A weak catalog photo can still rank the correct element first.
            # Require independent, distinct support from the labelled PDF index.
            if index and index[0] == record[0] and index[1] < .14 and index[2] > .09:
                return record, "Match via PDF-index", round(100*math.exp(-2*index[1]))
            if index and index[1] < .14 and index[2] > .09 and len(ranked) > 1:
                second_distance, second = ranked[1]
                if (second[0] == index[0] and second_distance-distance < .07
                        and self.other_variant_supports(visual, second)):
                    return second, "Match via PDF-index", round(100*math.exp(-2*index[1]))
            return None, "Onbekend", 0
        gap = ranked[1][0]-distance if len(ranked)>1 else 1.
        # Relative image score. A near tie reduces the score even for a close match.
        similarity = math.exp(-2 * max(0, distance))
        separation = min(1, gap / .12) if len(ranked) > 1 else 0
        score = round(100 * similarity * (.65 + .35 * separation))
        status = "Voorstel" if distance < .30 and gap > .055 else "Controleren"
        if index:
            number, index_distance, margin, _ = index
            if number == record[0]:
                status = "Ook in PDF-index"
            else:
                # Break a close Rebrickable tie only with a very distinctive index image.
                if index_distance < .14 and margin > .09:
                    for alternative_distance, alternative in ranked[1:3]:
                        if alternative[0] == number and alternative_distance < min(.38, distance+.07):
                            return alternative, "Ook in PDF-index", round(65*math.exp(-2*alternative_distance))
                status = "Conflict met PDF"
        return record, status, score
