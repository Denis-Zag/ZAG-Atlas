"""Разбиение и лексический поиск с нормализацией русских слов."""
import math
import re
from collections import Counter

STOP = set("как что это для при или где когда какой какие нужно можно меня нам ли по на в и с из к у о а не".split())


def tokens(text):
    words = re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е"))
    result = []
    for word in words:
        if len(word) < 3 or word in STOP:
            continue
        if re.search("[а-я]", word):
            word = re.sub(r"(иями|ами|ями|ого|ему|ому|ыми|ими|ией|ий|ый|ая|ое|ые|ой|ом|ем|ам|ах|ов|ев|ям|ях|ть|ти|у|а|ы|и|е|я|ю|о)$", "", word)
        result.append(word)
    return result


def split_text(text, size=1200):
    paragraphs = re.split(r"\n\s*\n", text.strip())
    result = []
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        while len(paragraph) > size:
            cut = paragraph.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            result.append(paragraph[:cut])
            paragraph = paragraph[cut:].strip()
        if paragraph:
            result.append(paragraph)
    return result


def retrieve(question, snippets, budget=20000, top_k=8):
    query = set(tokens(question))
    if not query:
        return []
    corpus = [Counter(tokens(s["snippet_text"])) for s in snippets]
    freq = Counter(token for doc in corpus for token in doc)
    ranked = []
    for snippet, counts in zip(snippets, corpus):
        score = sum((1 + math.log(counts[t])) *
                    math.log(1 + len(corpus) / (1 + freq[t]))
                    for t in query if counts[t])
        if score > 0:
            ranked.append((score, snippet))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    found, used = [], 0
    for _, snippet in ranked[:top_k]:
        if used + len(snippet["snippet_text"]) > budget:
            continue
        found.append(snippet)
        used += len(snippet["snippet_text"])
    return found
