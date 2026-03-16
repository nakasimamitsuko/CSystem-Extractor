/*
 * lzss_fast.c - C,system 引擎 LZSS 高速解压/压缩
 * 编译: python build_lzss.py
 * 或手动: gcc -O3 -shared -fPIC -o lzss_fast.so lzss_fast.c (Linux)
 *         cl /O2 /LD lzss_fast.c /Fe:lzss_fast.pyd (Windows)
 */

#include <stdlib.h>
#include <string.h>

#define WINDOW_SIZE     4096
#define WINDOW_MASK     (WINDOW_SIZE - 1)
#define MAX_MATCH_LEN   18
#define MATCH_THRESHOLD  2
#define CHAR_FILLER      0x00

/*
 * LZSS 解压
 * 返回实际解压字节数, -1 表示错误
 */
int lzss_decompress_c(
    const unsigned char *src, int src_len,
    unsigned char *dst, int dst_size)
{
    unsigned char ring[WINDOW_SIZE];
    int ring_pos = WINDOW_SIZE - MAX_MATCH_LEN;
    int src_pos = 0;
    int dst_pos = 0;
    unsigned int flags = 0;

    memset(ring, CHAR_FILLER, WINDOW_SIZE);

    while (dst_pos < dst_size) {
        flags >>= 1;
        if ((flags & 0x100) == 0) {
            if (src_pos >= src_len) break;
            flags = (unsigned int)src[src_pos++] | 0xFF00;
        }

        if (flags & 1) {
            /* literal */
            if (src_pos >= src_len) break;
            unsigned char b = src[src_pos++];
            dst[dst_pos++] = b;
            ring[ring_pos] = b;
            ring_pos = (ring_pos + 1) & WINDOW_MASK;
        } else {
            /* reference */
            if (src_pos + 1 >= src_len) break;
            unsigned char low = src[src_pos];
            unsigned char high = src[src_pos + 1];
            src_pos += 2;

            int offset = low | ((high & 0xF0) << 4);
            int length = (high & 0x0F) + MATCH_THRESHOLD + 1;

            for (int i = 0; i < length && dst_pos < dst_size; i++) {
                unsigned char b = ring[(offset + i) & WINDOW_MASK];
                dst[dst_pos++] = b;
                ring[ring_pos] = b;
                ring_pos = (ring_pos + 1) & WINDOW_MASK;
            }
        }
    }
    return dst_pos;
}

/*
 * LZSS 压缩 (带哈希加速匹配)
 * 返回压缩后字节数, -1 表示错误
 * dst 需要预分配足够空间 (最坏情况 src_len * 9/8 + 2)
 */

/* 简单哈希表加速匹配查找 */
#define HASH_SIZE 4096
#define HASH_MASK (HASH_SIZE - 1)
#define NIL       (-1)

static inline int hash3(const unsigned char *p) {
    return ((p[0] << 4) ^ (p[1] << 2) ^ p[2]) & HASH_MASK;
}

int lzss_compress_c(
    const unsigned char *src, int src_len,
    unsigned char *dst, int dst_max)
{
    unsigned char ring[WINDOW_SIZE];
    int ring_pos = WINDOW_SIZE - MAX_MATCH_LEN;
    int src_pos = 0;
    int dst_pos = 0;

    /* 哈希链表 */
    int head[HASH_SIZE];
    int prev[WINDOW_SIZE];
    memset(head, NIL, sizeof(head));
    memset(prev, NIL, sizeof(prev));
    memset(ring, CHAR_FILLER, WINDOW_SIZE);

    while (src_pos < src_len) {
        unsigned char flag_byte = 0;
        int flag_pos = dst_pos++;
        if (dst_pos > dst_max) return -1;

        int items_start = dst_pos;
        /* 临时缓冲flag组的数据 */
        unsigned char items[MAX_MATCH_LEN * 8 + 8];
        int items_len = 0;

        for (int bit = 0; bit < 8 && src_pos < src_len; bit++) {
            int best_offset = 0;
            int best_length = 0;
            int max_len = src_len - src_pos;
            if (max_len > MAX_MATCH_LEN) max_len = MAX_MATCH_LEN;

            if (max_len > MATCH_THRESHOLD && src_pos + 2 < src_len) {
                /* 哈希链搜索 */
                int h = hash3(src + src_pos);
                int chain = head[h];
                int chain_limit = 128; /* 限制链长度 */

                while (chain != NIL && chain_limit-- > 0) {
                    /* chain 是 ring 中的位置 */
                    int match_len = 0;
                    int rp = chain;
                    int sp = src_pos;
                    while (match_len < max_len &&
                           ring[rp & WINDOW_MASK] == src[sp]) {
                        match_len++;
                        rp++;
                        sp++;
                    }
                    if (match_len > best_length) {
                        best_length = match_len;
                        best_offset = chain & WINDOW_MASK;
                        if (best_length == max_len) break;
                    }
                    chain = prev[chain & WINDOW_MASK];
                }
            }

            if (best_length > MATCH_THRESHOLD) {
                /* reference */
                int low = best_offset & 0xFF;
                int high = ((best_offset >> 4) & 0xF0) |
                           ((best_length - MATCH_THRESHOLD - 1) & 0x0F);
                items[items_len++] = (unsigned char)low;
                items[items_len++] = (unsigned char)high;

                for (int i = 0; i < best_length; i++) {
                    unsigned char b = src[src_pos];
                    /* 更新哈希链 */
                    if (src_pos + 2 < src_len) {
                        int h = hash3(src + src_pos);
                        prev[ring_pos & WINDOW_MASK] = head[h];
                        head[h] = ring_pos;
                    }
                    ring[ring_pos] = b;
                    ring_pos = (ring_pos + 1) & WINDOW_MASK;
                    src_pos++;
                }
            } else {
                /* literal */
                flag_byte |= (1 << bit);
                unsigned char b = src[src_pos];
                items[items_len++] = b;

                if (src_pos + 2 < src_len) {
                    int h = hash3(src + src_pos);
                    prev[ring_pos & WINDOW_MASK] = head[h];
                    head[h] = ring_pos;
                }
                ring[ring_pos] = b;
                ring_pos = (ring_pos + 1) & WINDOW_MASK;
                src_pos++;
            }
        }

        dst[flag_pos] = flag_byte;
        if (dst_pos + items_len > dst_max) return -1;
        memcpy(dst + dst_pos, items, items_len);
        dst_pos += items_len;
    }

    return dst_pos;
}
