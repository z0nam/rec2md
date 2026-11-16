"""회의 시리즈 맥락: 용어집·참석자·자료 → 전사 프롬프트와 보정 후보의 재료.

    ~/.config/rec2md/series/<이름>/
        glossary.txt    한 줄에 한 항목. `오인식 => 정답` 형식도 받는다. `#`로 시작하면 주석
        attendees.txt   참석자(원내·외부 구분 없음). 한 줄에 `이름` 또는 `이름<TAB>소속`.
                        탭이 없는 줄은 공백으로 나열한 이름들로 본다(클로바 참석자 줄을 그대로 붙여넣은 경우)
        docs/           안건·발표자료·이전 회의록 (md·txt·hwpx·docx·pdf)

`--context <파일...>`로 시리즈 없이 이번 회의 자료만 줄 수도 있다. 자료 본문은 그대로 프롬프트에
넣지 않고 용어 후보(자주 나오는 고유명사꼴 토큰)만 뽑아 쓴다.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PROMPT_TOKENS = 200  # Whisper 프롬프트 상한은 223토큰. 길수록 무음 구간에 프롬프트가 튀어나올 위험이 커진다
DOC_EXTS = {".md", ".txt", ".hwpx", ".docx", ".pdf"}


def series_root() -> Path:
    return Path(os.environ.get("REC2MD_SERIES_DIR", Path.home() / ".config" / "rec2md" / "series"))


@dataclass
class Context:
    terms: list[str] = field(default_factory=list)  # 정답 표기 (용어집 순서 유지)
    variants: dict[str, str] = field(default_factory=dict)  # 오인식 → 정답
    attendees: list[str] = field(default_factory=list)
    doc_terms: list[str] = field(default_factory=list)  # 자료에서 뽑은 후보 (빈도순)

    def merge(self, other: Context) -> Context:
        uniq = lambda xs: list(dict.fromkeys(xs))
        return Context(uniq(self.terms + other.terms), {**self.variants, **other.variants},
                       uniq(self.attendees + other.attendees), uniq(self.doc_terms + other.doc_terms))

    def vocabulary(self) -> list[str]:
        """보정 후보 비교에 쓸 정답 표기 전체."""
        return list(dict.fromkeys(self.attendees + self.terms + self.doc_terms))

    def __bool__(self) -> bool:
        return bool(self.terms or self.attendees or self.doc_terms)


def parse_glossary(text: str) -> tuple[list[str], dict[str, str]]:
    terms: list[str] = []
    variants: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" in line:
            wrong, right = (x.strip() for x in line.split("=>", 1))
            if right:
                variants[wrong] = right
                terms.append(right)
        else:
            terms.append(line)
    return list(dict.fromkeys(terms)), variants


def parse_attendees_full(text: str) -> dict[str, str]:
    """참석자 → 소속(모르면 ""). 명부에 없는 외부인도 그대로 받는다."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if "\t" in line:
            name, org = (x.strip() for x in line.split("\t", 1))
            if name:
                out.setdefault(name, org)
        else:
            for n in line.split():
                out.setdefault(n, "")
    return out


_UNNAMED = re.compile(r"^(Attendees|참석자|화자|Speaker)\s*\d+$", re.I)
_NAME = re.compile(r"^[가-힣]{2,4}$")


def attendees_from_clova(path: Path) -> dict[str, str]:
    """클로바 txt의 화자 라벨 → {이름: 소속}. 등장 순서대로.

    화자 라벨은 사용자가 붙인 것이라 믿을 수 있지만 "홍길동 ○○도청 ○○팀장"처럼 소속이 붙기도 한다.
    3번째 줄(참석자 목록)은 이런 라벨을 공백으로 이어 붙인 것이라 이름 경계를 알 수 없으므로 쓰지 않는다.
    이름을 붙이지 않은 라벨("Attendees 3")과 이름이 아닌 라벨("○○ 주무관님 (성함확인필요)")은 건너뛴다.
    """
    from .clova_txt import parse

    out: dict[str, str] = {}
    for seg in parse(path).segments:
        label = (seg.speaker or "").strip()
        if not label or _UNNAMED.match(label) or "확인" in label:
            continue
        name, _, org = label.partition(" ")
        if _NAME.match(name):
            out.setdefault(name, org.strip())
    return out


def parse_attendees(text: str) -> list[str]:
    return list(parse_attendees_full(text))


# ---- 자료 텍스트 추출 ----

def _xml_text(xml: bytes) -> str:
    # 문단 경계(<w:p>, <hp:p>)에서 줄을 바꾸고 나머지 태그는 지운다
    s = re.sub(rb"</(?:w|hp):p>", b"\n", xml)
    return re.sub(r"<[^>]+>", "", s.decode("utf-8", "ignore"))


def doc_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".md", ".txt"}:
        return path.read_text(encoding="utf-8-sig", errors="ignore")
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            return _xml_text(z.read("word/document.xml"))
    if ext == ".hwpx":
        if shutil.which("hwp-agent"):
            r = subprocess.run(["hwp-agent", "extract", str(path)], capture_output=True, text=True, check=False)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        with zipfile.ZipFile(path) as z:  # hwp-agent가 없으면 본문 XML을 직접 읽는다
            names = sorted(n for n in z.namelist() if re.match(r"Contents/section\d+\.xml$", n))
            return "\n".join(_xml_text(z.read(n)) for n in names)
    if ext == ".pdf":
        if not shutil.which("pdftotext"):
            raise SystemExit("pdf 자료는 pdftotext(poppler)가 필요함: brew install poppler")
        return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True,
                              check=True).stdout
    raise SystemExit(f"지원하지 않는 자료 형식: {path.name} ({', '.join(sorted(DOC_EXTS))})")


# 조사·어미를 떼어 같은 낱말을 한 번으로 센다. 긴 것부터 맞춘다
_JOSA = sorted("은 는 이 가 을 를 의 에 에서 에게 으로 로 와 과 도 만 까지 부터 이나 나 이랑 랑 하고 처럼 보다 이다 입니다 에는 에서는 으로는 로는 과의 와의 이라는 라는".split(),
               key=len, reverse=True)
# 문서에 흔하지만 용어가 아닌 낱말
_STOP = set("""그리고 그러나 하지만 또한 및 등 통해 대한 위한 관련 경우 이번 해당 다음 현재 기존 추가 필요 검토 내용 사항
진행 계획 결과 방안 부분 정도 이상 이하 우리 여기 거기 이것 그것 때문 따라 대해 위해 있음 없음 함께 모든 각각 기타 참고
""".split())
_TOKEN = re.compile(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9·\-]*[가-힣A-Za-z0-9]")


def _split_josa(w: str) -> tuple[str, str]:
    for j in _JOSA:
        if w.endswith(j) and len(w) - len(j) >= 2:
            return w[: -len(j)], j
    return w, ""


def _strip_josa(w: str) -> str:
    return _split_josa(w)[0]


# 용언 활용꼴로 끝나는 토큰("되는데", "했는데", "같아요")은 용어가 아니다
_VERBAL = re.compile(r"(요|다|데|면|서|게|지|까|죠|고|던|된|한|할|는|든|며|니)$")


def _looks_term(w: str) -> bool:
    if re.search(r"[A-Za-z]", w):  # 약어·영문 섞인 이름(ABC, AI허브)은 짧아도 남긴다
        return True
    # 한글 2음절은 일반 낱말이 대부분이라 뺀다. 2음절 인명·기관명은 용어집에 직접 적는다
    return len(w) >= 3 and not _VERBAL.search(w) and not w.isdigit()


# "○○고"(고등학교 약칭)는 끝 글자가 연결어미 "-고"와 같아 _VERBAL 에 걸린다(#47).
# 체언 뒤에만 붙는 조사로 나온 적이 있으면 명사로 보고 살린다. "-고는"·"-고도"·"-고만"·"-고부터"는
# 용언 활용("하고는", "알고도")에도 흔하므로 근거로 쓰지 않는다
_NOUN_JOSA = frozenset("가 를 의 에 에서 에게 로 와 와의 에는 에서는 로는 보다 처럼 라는 랑".split())


def _school_abbrevs(text: str, toks: list[tuple[str, str]]) -> set[str]:
    """학교 약칭으로 볼 "○○고": 자료에 "○○고등학교"가 함께 나오거나, 체언 조사가 붙은 꼴이 2번 이상."""
    noun = Counter(w for w, j in toks if j in _NOUN_JOSA and w.endswith("고"))
    out = {w for w, c in noun.items() if c >= 2}
    out |= {m + "고" for m in re.findall(r"([가-힣]{2,})고등학교", text)}
    return out


# 영어 기능어·흔한 낱말. 영어 자료에서는 빈도순 상위를 이런 낱말이 차지한다
_EN_STOP = frozenset("""a an the and or but nor so yet for of in on at to from by with without into onto over under
about above below after before between through during within against among across along around
is am are was were be been being do does did done have has had having can could will would shall should
may might must not no yes it its this that these those there here then than as if when where while what
which who whom whose why how all any each every both few more most other some such only own same too very
just also again once i me my we our you your he him his she her they them their one two three
use used using get got make made see new way like well many much first last next page slide slides
answer question questions example examples note notes lecture class week today student students
therefore however thank thanks everything main part set result results output input table figure section appendix
introduction background method methods methodology analysis conclusion conclusions limitation limitations
future direction directions problem objective proposed research summary overview discussion experiment experiments
""".split())  # 뒤 두 줄: 발표 자료의 장 제목(BACKGROUND, Conclusion 등)
_LATIN = re.compile(r"[A-Za-z]")
_CAMEL_ARTIFACT = re.compile(r"^[a-z]{2,}[A-Z]")  # notesSlides 같은 슬라이드 파일 내부 이름. iPhone·eBay는 남는다


def _latin_term(w: str, lower_seen: set[str]) -> bool:
    """영문 토큰이 용어다운가: 약어(TDX)·숫자 섞임(GPT4)·고유명사(Bayes)만. 소문자 일반 낱말은 뺀다.

    문장 첫머리라 대문자로 시작한 일반 낱말("Students")은 자료 어딘가에 소문자로도 나오므로 그걸로 거른다."""
    if w.lower() in _EN_STOP or _CAMEL_ARTIFACT.match(w):
        return False
    if not re.search(r"[A-Z0-9]", w):
        return False
    if w[0].isupper() and w[1:].islower() and w.lower() in lower_seen:
        return False
    return len(w) >= 3 or w.isupper()


def extract_terms(text: str, limit: int = 60, min_count: int = 2) -> list[str]:
    """자료 본문 → 용어 후보. 형태소 분석 없이 두 번 이상 나온 3〜15자 토큰을 빈도·길이순으로.

    한글이 없는 영문 토큰은 _latin_term으로 따로 거른다(영어 자료의 불용어·일반 낱말 제외)."""
    toks = [_split_josa(tok) for tok in _TOKEN.findall(text)]
    schools = _school_abbrevs(text, toks)
    lower_seen = {w for w, _ in toks if w.islower()}
    counts: Counter[str] = Counter()
    for w, _ in toks:
        if len(w) > 15 or w in _STOP:
            continue
        if not re.search(r"[가-힣]", w) and _LATIN.search(w):
            ok = _latin_term(w, lower_seen)
        else:
            ok = _looks_term(w) or (w in schools and len(w) >= 3)
        if ok:
            counts[w] += 1
    ranked = sorted((w for w, c in counts.items() if c >= min_count), key=lambda w: (-counts[w] * len(w), w))
    return ranked[:limit]


def load_docs(paths: list[Path]) -> Context:
    text = "\n".join(doc_text(p) for p in paths)
    return Context(doc_terms=extract_terms(text))


def load_series(name: str) -> Context:
    d = series_root() / name
    if not d.is_dir():
        raise SystemExit(f"시리즈 없음: {name} (rec2md series init {name})")
    ctx = Context()
    if (g := d / "glossary.txt").is_file():
        ctx.terms, ctx.variants = parse_glossary(g.read_text(encoding="utf-8"))
    if (a := d / "attendees.txt").is_file():
        ctx.attendees = parse_attendees(a.read_text(encoding="utf-8"))
    docs = sorted(p for p in (d / "docs").glob("*") if p.suffix.lower() in DOC_EXTS) if (d / "docs").is_dir() else []
    if docs:
        ctx = ctx.merge(load_docs(docs))
    return ctx


# ---- 프롬프트 ----

@functools.cache
def _tokenizer():
    try:
        from mlx_whisper.tokenizer import get_tokenizer
        return get_tokenizer(True, num_languages=100)
    except ImportError:
        return None


def _count_tokens(text: str) -> int:
    tok = _tokenizer()
    # mlx-whisper가 없으면 글자 수로 센다. 한국어는 대략 글자당 0.9토큰이라 약간 넉넉하다
    return len(tok.encode(" " + text)) if tok else len(text)


EN_LEAD = "Today we will talk about "  # 영어 프롬프트의 문장 틀. clean이 메아리 판정 때 떼어 낸다
EN_MIN_TERMS = 3  # 영어에서 걸러진 용어가 이보다 적으면 프롬프트를 넣지 않는다


def build_prompt(ctx: Context, max_tokens: int = PROMPT_TOKENS, language: str | None = "ko") -> str:
    """참석자 → 용어집 → 자료 후보 순으로, 상한을 넘기 전까지 잇는다.

    한국어(기본)는 쉼표로 나열한다. 영어는 Whisper가 프롬프트의 문체(쉼표 남발 등)를 흉내 내므로 평범한 구어 문장
    한 줄로 만든다: "Today we will talk about X, Y, and Z." 한글 항목은 영어 전사를 한국어 쪽으로 끌 수 있어 뺀다.
    """
    items = list(dict.fromkeys(ctx.attendees + ctx.terms + ctx.doc_terms))
    if language != "en":
        out: list[str] = []
        for item in items:
            if _count_tokens(", ".join(out + [item])) > max_tokens:
                break
            out.append(item)
        return ", ".join(out)
    items = [x for x in items if not re.search(r"[가-힣]", x)]
    out = []
    for item in items:
        if _count_tokens(_en_sentence(out + [item])) > max_tokens:
            break
        out.append(item)
    return _en_sentence(out) if len(out) >= EN_MIN_TERMS else ""


def _en_sentence(items: list[str]) -> str:
    if not items:
        return ""
    body = items[0] if len(items) == 1 else ", ".join(items[:-1]) + ", and " + items[-1]
    return f"{EN_LEAD}{body}."


def prompt_items(prompt: str) -> list[str]:
    """프롬프트 → 항목 목록(한국어 쉼표 나열과 영어 문장 둘 다)."""
    if prompt.startswith(EN_LEAD):
        prompt = prompt[len(EN_LEAD):].rstrip(".")
    return [re.sub(r"^and ", "", x.strip()) for x in prompt.split(",") if x.strip()]
