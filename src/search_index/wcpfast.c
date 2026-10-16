/* ==========================================================================
   wcpfast — the tokeniser hot loop in C, behind a plain C ABI.
   --------------------------------------------------------------------------
   ``analyze()`` runs once per indexed document. Over a Common Crawl snapshot
   that is billions of trips through a Python-level loop that lowercases a
   token, tests it against a stopword set and strips a suffix. This file does
   the same work in one call.

   Why a plain C ABI instead of a CPython extension module:

   * no Python headers at build time, so it compiles anywhere gcc exists —
     including slim containers that ship Python without python3-dev;
   * no ABI tag, so one ``.so`` works across CPython versions instead of
     having to be rebuilt per interpreter;
   * ctypes carries the argument marshalling, so the Python side stays a
     readable module rather than 400 lines of reference counting.

   The rule that governs this file: **it must never change an answer.**

   * It handles ASCII only. The first non-ASCII byte makes it return
     WCP_NOT_ASCII and the caller uses the Python implementation, which is
     Unicode-correct by construction. Lowercasing in C equals Python's
     ``str.lower()`` only for ASCII, and Python's ``\w`` matches a
     Unicode set we are not going to reimplement.
   * The stopword table and suffix rules are passed in from Python at
     configure time, so they cannot drift out of sync with ``text.py``.
   * If the library is missing, unbuilt or declines, behaviour is exactly
     what it was before.

   Build:  scripts/build-native.sh        (optional; needs a C compiler)
   ========================================================================== */

#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#define WCP_NOT_ASCII ((size_t)-1)
#define WCP_TOO_SMALL ((size_t)-2)

#define MAX_RULES 32

#define SCRATCH_STACK 512

typedef struct {
    char stack[SCRATCH_STACK];
    char *heap;
    size_t heap_cap;
} scratch;

static char *scratch_get(scratch *s, size_t need) {
    if (need <= SCRATCH_STACK) return s->stack;
    if (s->heap_cap < need) {
        char *p = (char *)realloc(s->heap, need);
        if (!p) return NULL;
        s->heap = p;
        s->heap_cap = need;
    }
    return s->heap;
}

static void scratch_free(scratch *s) {
    free(s->heap);
    s->heap = NULL;
    s->heap_cap = 0;
}

static char **g_stopwords = NULL;
static size_t g_stopword_count = 0;

static char *g_rule_suffix[MAX_RULES];
static char *g_rule_replacement[MAX_RULES];
static size_t g_rule_count = 0;

/* ------------------------------------------------------------ configure -- */
/* Split a NUL-separated, double-NUL-terminated blob into a heap array. */
/* strdup() is POSIX rather than C99, so it is not declared under -std=c99.
   Rolling our own keeps the file portable to any C99 compiler. */
static char *dup_str(const char *s) {
    size_t n = strlen(s) + 1;
    char *out = (char *)malloc(n);
    if (out) memcpy(out, s, n);
    return out;
}

static char **split_nul(const char *blob, size_t *out_count) {
    size_t count = 0;
    const char *p = blob;
    while (*p) {
        count++;
        p += strlen(p) + 1;
    }
    if (count == 0) {
        *out_count = 0;
        return NULL;
    }
    char **out = (char **)calloc(count, sizeof(char *));
    if (!out) return NULL;
    p = blob;
    for (size_t i = 0; i < count; i++) {
        out[i] = dup_str(p);
        if (!out[i]) {
            for (size_t j = 0; j < i; j++) free(out[j]);
            free(out);
            return NULL;
        }
        p += strlen(p) + 1;
    }
    *out_count = count;
    return out;
}

static void free_stopwords(void) {
    for (size_t i = 0; i < g_stopword_count; i++) free(g_stopwords[i]);
    free(g_stopwords);
    g_stopwords = NULL;
    g_stopword_count = 0;
}

static void free_rules(void) {
    for (size_t i = 0; i < g_rule_count; i++) {
        free(g_rule_suffix[i]);
        free(g_rule_replacement[i]);
    }
    g_rule_count = 0;
}

static int cmp_cstr(const void *a, const void *b) {
    return strcmp(*(const char *const *)a, *(const char *const *)b);
}

/* wcp_configure(stopwords_blob, rules_blob)
     stopwords: NUL-separated, double-NUL terminated
     rules:     NUL-separated "suffix=replacement", double-NUL terminated
   Returns 1 on success, 0 on failure. */
int wcp_configure(const char *stopwords, const char *rules) {
    free_stopwords();
    free_rules();

    g_stopwords = split_nul(stopwords, &g_stopword_count);
    if (g_stopword_count && !g_stopwords) return 0;
    qsort(g_stopwords, g_stopword_count, sizeof(char *), cmp_cstr);

    size_t n = 0;
    const char *p = rules;
    while (*p) {
        if (n >= MAX_RULES) break;
        const char *eq = strchr(p, '=');
        if (!eq) { p += strlen(p) + 1; continue; }
        size_t sl = (size_t)(eq - p);
        g_rule_suffix[n] = (char *)malloc(sl + 1);
        if (!g_rule_suffix[n]) return 0;
        memcpy(g_rule_suffix[n], p, sl);
        g_rule_suffix[n][sl] = '\0';
        g_rule_replacement[n] = dup_str(eq + 1);
        if (!g_rule_replacement[n]) return 0;
        n++;
        p += strlen(p) + 1;
    }
    g_rule_count = n;
    return 1;
}

int wcp_enabled(void) {
    return g_rule_count > 0 ? 1 : 0;
}

/* --------------------------------------------------------------- helpers -- */
static int is_word_char(unsigned char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
           (c >= '0' && c <= '9') || c == '_';
}

static int all_ascii(const char *s, size_t n) {
    for (size_t i = 0; i < n; i++) {
        if ((unsigned char)s[i] >= 0x80) return 0;
    }
    return 1;
}

static int is_stopword(const char *tok) {
    size_t lo = 0, hi = g_stopword_count;      /* exclusive upper bound */
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;
        int c = strcmp(tok, g_stopwords[mid]);
        if (c == 0) return 1;
        if (c < 0) hi = mid;
        else lo = mid + 1;
    }
    return 0;
}

/* Checksums and ids are not words: a token of 7+ digits is dropped. */
static int is_long_digit(const char *tok, size_t len) {
    if (len <= 6) return 0;
    for (size_t i = 0; i < len; i++) {
        if (tok[i] < '0' || tok[i] > '9') return 0;
    }
    return 1;
}

/* In-place suffix strip. Mirrors text.py's stem(): first matching rule wins,
   and the stem left behind must be at least 4 characters. */
static void stem_token(char *tok, size_t len) {
    if (len < 5) return;
    for (size_t i = 0; i < g_rule_count; i++) {
        size_t sl = strlen(g_rule_suffix[i]);
        if (sl > len) continue;
        if (len - sl < 4) continue;
        if (memcmp(tok + (len - sl), g_rule_suffix[i], sl) != 0) continue;
        size_t rl = strlen(g_rule_replacement[i]);
        memmove(tok + (len - sl), g_rule_replacement[i], rl);
        tok[(len - sl) + rl] = '\0';
        return;
    }
}

/* Append a NUL-terminated string to the output buffer. */
static int emit(char *out, size_t cap, size_t *used, const char *s, size_t len) {
    if (*used + len + 1 > cap) return 0;
    memcpy(out + *used, s, len);
    out[*used + len] = '\0';
    *used += len + 1;
    return 1;
}

/* -------------------------------------------------------------- scanning -- */
typedef struct {
    const char *s;
    size_t n;
    size_t i;
} scanner;

static void scan_next(scanner *sc, const char **tok, size_t *len) {
    while (sc->i < sc->n && !is_word_char((unsigned char)sc->s[sc->i])) sc->i++;
    size_t start = sc->i;
    while (sc->i < sc->n && is_word_char((unsigned char)sc->s[sc->i])) sc->i++;
    *tok = sc->s + start;
    *len = sc->i - start;
}

static void lowercase_into(char *dst, const char *src, size_t len) {
    for (size_t i = 0; i < len; i++) {
        char c = src[i];
        dst[i] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c;
    }
    dst[len] = '\0';
}

/* wcp_tokenize -> NUL-separated lower-cased tokens. */
size_t wcp_tokenize(const char *in, size_t in_len, char *out, size_t cap) {
    if (!all_ascii(in, in_len)) return WCP_NOT_ASCII;
    scanner sc = {in, in_len, 0};
    scratch scratch_space = {{0}, NULL, 0};
    size_t used = 0;

    for (;;) {
        const char *tok;
        size_t len;
        scan_next(&sc, &tok, &len);
        if (len == 0) break;
        char *buf = scratch_get(&scratch_space, len + 1);
        if (!buf) { scratch_free(&scratch_space); return WCP_TOO_SMALL; }
        lowercase_into(buf, tok, len);
        if (!emit(out, cap, &used, buf, len)) {
            scratch_free(&scratch_space);
            return WCP_TOO_SMALL;
        }
    }
    scratch_free(&scratch_space);
    return used;
}

/* wcp_analyze -> NUL-separated stems. drop_stopwords=0 keeps stopwords. */
size_t wcp_analyze(const char *in, size_t in_len, char *out, size_t cap,
                   int drop_stopwords) {
    if (!all_ascii(in, in_len)) return WCP_NOT_ASCII;
    scanner sc = {in, in_len, 0};
    scratch scratch_space = {{0}, NULL, 0};
    size_t used = 0;

    for (;;) {
        const char *tok;
        size_t len;
        scan_next(&sc, &tok, &len);
        if (len == 0) break;
        char *buf = scratch_get(&scratch_space, len + 1);
        if (!buf) { scratch_free(&scratch_space); return WCP_TOO_SMALL; }
        lowercase_into(buf, tok, len);
        if (drop_stopwords && is_stopword(buf)) continue;
        if (is_long_digit(buf, len)) continue;
        stem_token(buf, len);
        if (!emit(out, cap, &used, buf, strlen(buf))) {
            scratch_free(&scratch_space);
            return WCP_TOO_SMALL;
        }
    }
    scratch_free(&scratch_space);
    return used;
}

/* wcp_analyze_pairs -> NUL-separated "stem", "surface", "stem", "surface"… */
size_t wcp_analyze_pairs(const char *in, size_t in_len, char *out, size_t cap) {
    if (!all_ascii(in, in_len)) return WCP_NOT_ASCII;
    scanner sc = {in, in_len, 0};
    scratch scratch_space = {{0}, NULL, 0};
    size_t used = 0;

    for (;;) {
        const char *tok;
        size_t len;
        scan_next(&sc, &tok, &len);
        if (len == 0) break;
        char *buf = scratch_get(&scratch_space, (len + 1) * 2);
        if (!buf) { scratch_free(&scratch_space); return WCP_TOO_SMALL; }
        char *surface = buf + (len + 1);
        lowercase_into(buf, tok, len);
        if (is_stopword(buf)) continue;
        if (is_long_digit(buf, len)) continue;
        memcpy(surface, buf, len + 1);
        stem_token(buf, len);
        if (!emit(out, cap, &used, buf, strlen(buf))) {
            scratch_free(&scratch_space);
            return WCP_TOO_SMALL;
        }
        if (!emit(out, cap, &used, surface, len)) {
            scratch_free(&scratch_space);
            return WCP_TOO_SMALL;
        }
    }
    scratch_free(&scratch_space);
    return used;
}
