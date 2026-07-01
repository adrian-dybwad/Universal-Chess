/* Centaur display-translation shim (LD_PRELOAD).
 *
 * Purpose
 * -------
 * Make the original DGT Centaur software render through Universal-Chess's
 * driver stack instead of its own panel, so it works on whatever e-paper is
 * installed (incl. an incompatible controller or a three-color panel).
 *
 * centaur (a 32-bit Nuitka binary) drives its panel two ways, established by
 * static inspection of its bundle:
 *   - SPI  via bundled spidev.so  -> write()/writev()/ioctl(SPI_IOC_MESSAGE) on
 *           /dev/spidevN.M
 *   - GPIO via bundled RPi/_GPIO.so -> mmap() of /dev/gpiomem, then direct reads
 *           (BUSY) and writes (DC/RST/CS) of the GPSET0/GPCLR0/GPLEV0 registers.
 *
 * This shim:
 *   0. Virtualizes /proc/cpuinfo and /dev/gpiomem when the host needs it, gated
 *      by TWO INDEPENDENT flags decided once at load (see shim_init):
 *        - `spoof_cpuinfo` presents a synthetic cpuinfo (see fopen hook) when the
 *          real /proc/cpuinfo lacks a "Hardware :" line. centaur's bundled
 *          RPi/_GPIO.so (an old armhf build) identifies the SoC from that line
 *          and raises "This module can only be run on a Raspberry Pi!" at import
 *          if it is absent. A 64-bit Raspberry Pi OS kernel drops the line, so
 *          this is needed on EVERY arm64 host -- the Pi Zero 2 W and Pi 4 just as
 *          much as the CM5 / Pi 5 -- even those with a legacy /dev/gpiomem.
 *        - `spoof_gpiomem` substitutes for a missing /dev/gpiomem (see open hook)
 *          only on the RP1 boards (Pi 5 / CM5) that removed the legacy device.
 *      They are decoupled because the two conditions do not coincide: the Zero 2
 *      W has a legacy /dev/gpiomem (no substitute needed) but a 64-bit cpuinfo
 *      (synthetic cpuinfo needed). On a native 32-bit host the kernel includes
 *      the Hardware line and the legacy /dev/gpiomem, so both flags stay off and
 *      the fopen/open hooks pass straight through -- native hardware unaffected.
 *   1. Virtualizes /dev/gpiomem: centaur's mmap is redirected to a private
 *      shadow page, so its DC/RST/CS writes never reach the real pins (no
 *      contention with UC, which keeps driving the real panel) and its BUSY
 *      reads always return "idle" (so its driver never times out waiting on a
 *      panel it cannot see). The shadow is write-protected so the SIGSEGV
 *      handler can track the DC line for SPI tagging.
 *   2. Swallows centaur's real SPI transfers (they never reach the bus) and
 *      instead forwards each transfer -- tagged with the DC line state -- to the
 *      UC gateway over a unix socket, where it is decoded back into a framebuffer
 *      and rendered on the installed panel.
 *
 * Wire format (matches services/centaur_display/protocol.py):
 *      uint8 dc ; uint32 length (LE) ; length bytes payload
 *
 * Environment:
 *   UC_CENTAUR_DISPLAY_SOCK  gateway socket path (default below)
 *   UC_CENTAUR_BUSY_IDLE_HIGH 1 if BUSY idle is logic HIGH (UC8151D, default),
 *                             0 if idle is LOW (SSD1680). Tuned per controller.
 *
 * STATUS: the SPI swallow + socket forwarding and the gpiomem shadow / BUSY
 * seeding are straightforward. The DC write-trap relies on decoding the faulting
 * ARM store; the A32 path is implemented, Thumb degrades safely (see handler).
 * This requires on-hardware validation with centaur running; it cannot be
 * exercised off-device.
 *
 * BCM pins (UC epdconfig wiring): DC=16 RST=12 BUSY=7 CS=18.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <dlfcn.h>
#include <stdarg.h>
#include <pthread.h>
#include <signal.h>
#include <errno.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <sys/uio.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <ucontext.h>
#include <sys/syscall.h>
#include <linux/spi/spidev.h>

/* ---- BCM pin numbers (match epdconfig) ---------------------------------- */
#define PIN_DC   16
#define PIN_RST  12
#define PIN_BUSY 7
#define PIN_CS   18

/* ---- GPIO register word indices (BCM2835/2711 GPIO block) --------------- */
#define GPSET0 7   /* write 1-bits -> drive pin high */
#define GPCLR0 10  /* write 1-bits -> drive pin low  */
#define GPLEV0 13  /* read -> current pin levels     */

#define DEFAULT_SOCK "/run/universalchess/centaur-display.sock"

/* ---- real libc entry points --------------------------------------------- */
static ssize_t (*real_write)(int, const void *, size_t);
static ssize_t (*real_writev)(int, const struct iovec *, int);
static int     (*real_ioctl)(int, unsigned long, ...);
static void   *(*real_mmap)(void *, size_t, int, int, int, off_t);
static FILE   *(*real_fopen)(const char *, const char *);
static FILE   *(*real_fopen64)(const char *, const char *);
static int     (*real_open)(const char *, int, ...);
static int     (*real_open64)(const char *, int, ...);

/* ---- shim state --------------------------------------------------------- */
static pthread_mutex_t lk = PTHREAD_MUTEX_INITIALIZER;
static int   gw_fd = -1;            /* gateway socket, -1 until connected */
static int   busy_idle_high = 1;    /* BUSY idle logic level             */

static volatile uint32_t *gpio_shadow = NULL; /* base of the shadow page */
static size_t gpio_shadow_len = 0;
static long   page_size = 4096;

/* The two board-compatibility spoofs, gated INDEPENDENTLY (see shim_init for the
 * rationale and detection). Each stays 0 on a host that does not need it, so the
 * corresponding hook falls straight through to libc with one branch of overhead.
 *   spoof_cpuinfo: present a synthetic /proc/cpuinfo (fopen hook) -- set when the
 *                  real cpuinfo lacks a "Hardware :" line (any 64-bit RPi kernel).
 *   spoof_gpiomem: substitute for a missing /dev/gpiomem (open hook) -- set when
 *                  the legacy device is absent (the RP1 Pi 5 / CM5 boundary). */
static int spoof_cpuinfo = 0;
static int spoof_gpiomem = 0;

/* Tracked virtual pin levels (only the four we drive matter). */
static uint32_t pin_levels = 0;     /* bit N = level of BCM pin N */

/* fds returned in place of /dev/gpiomem. The legacy /dev/gpiomem does not exist
 * on the Pi 5 / CM5 (only per-bank /dev/gpiomemN for the RP1), so centaur's
 * open("/dev/gpiomem") gets ENOENT and RPi.GPIO falls back to /dev/mem, which
 * needs root and fails ("No access to /dev/mem"). The open hook below satisfies
 * that open with a mappable substitute (/dev/zero) and records the fd here so
 * the mmap hook shadows it -- readlink would show the substitute, not
 * /dev/gpiomem, so path detection alone would miss it. Only the first gpiomem
 * mmap matters (see the gpio_shadow guard in mmap), so a few slots suffice. */
#define MAX_FAKE_GPIOMEM_FDS 8
static int fake_gpiomem_fds[MAX_FAKE_GPIOMEM_FDS];

static void fake_gpiomem_add(int fd) {
    pthread_mutex_lock(&lk);
    for (int i = 0; i < MAX_FAKE_GPIOMEM_FDS; i++) {
        if (fake_gpiomem_fds[i] < 0) { fake_gpiomem_fds[i] = fd; break; }
    }
    pthread_mutex_unlock(&lk);
}

static int fake_gpiomem_has(int fd) {
    int found = 0;
    pthread_mutex_lock(&lk);
    for (int i = 0; i < MAX_FAKE_GPIOMEM_FDS; i++) {
        if (fake_gpiomem_fds[i] == fd) { found = 1; break; }
    }
    pthread_mutex_unlock(&lk);
    return found;
}

/* ---- opt-in debug (UC_CENTAUR_SHIM_DEBUG=/path) ------------------------- */
static int  dbg_fd = -1;            /* debug log fd, -1 if disabled */
static volatile unsigned long dbg_faults = 0, dbg_decoded = 0,
                              dbg_thumb = 0, dbg_unknown = 0, dbg_forwards = 0;

/* Raw-syscall write so debug logging never re-enters our write() hook and is
 * safe to call from the SIGSEGV handler. */
static void dbg_emit(const char *s) {
    if (dbg_fd < 0) return;
    size_t n = 0; while (s[n]) n++;
    syscall(SYS_write, dbg_fd, s, n);
}

/* DC tag used for the next SPI transfer: 0 = command, 1 = data. */
static int dc_state = 0;

/* -------------------------------------------------------------------------
 * Gateway socket
 * ---------------------------------------------------------------------- */
static void gw_connect_locked(void) {
    if (gw_fd >= 0) return;
    const char *path = getenv("UC_CENTAUR_DISPLAY_SOCK");
    if (!path || !*path) path = DEFAULT_SOCK;

    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return;
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        close(fd);
        return;
    }
    gw_fd = fd;
}

static void gw_send_all_locked(const unsigned char *buf, size_t n) {
    if (gw_fd < 0) return;
    size_t off = 0;
    while (off < n) {
        ssize_t w = send(gw_fd, buf + off, n - off, MSG_NOSIGNAL);
        if (w <= 0) {            /* gateway gone: drop the connection, never block centaur */
            close(gw_fd);
            gw_fd = -1;
            return;
        }
        off += (size_t)w;
    }
}

/* Forward one SPI transfer to the gateway, tagged with the current DC state. */
static void forward_xfer(const unsigned char *buf, uint32_t len) {
    if (!len) return;
    pthread_mutex_lock(&lk);
    gw_connect_locked();
    unsigned char hdr[5];
    hdr[0] = (unsigned char)(dc_state ? 1 : 0);
    hdr[1] = (unsigned char)(len & 0xFF);
    hdr[2] = (unsigned char)((len >> 8) & 0xFF);
    hdr[3] = (unsigned char)((len >> 16) & 0xFF);
    hdr[4] = (unsigned char)((len >> 24) & 0xFF);
    gw_send_all_locked(hdr, sizeof(hdr));
    gw_send_all_locked(buf, len);
    dbg_forwards++;
    if (dbg_forwards <= 60) {
        char b[96];
        snprintf(b, sizeof(b), "[shim] forward #%lu dc=%d len=%u first=0x%02x\n",
                 dbg_forwards, dc_state, len, buf[0]);
        dbg_emit(b);
    }
    pthread_mutex_unlock(&lk);
}

/* -------------------------------------------------------------------------
 * spidev fd identification (avoids tracking open() across the LFS alias mess)
 * ---------------------------------------------------------------------- */
static int is_spidev_fd(int fd) {
    char p[64], target[256];
    snprintf(p, sizeof(p), "/proc/self/fd/%d", fd);
    ssize_t r = readlink(p, target, sizeof(target) - 1);
    if (r <= 0) return 0;
    target[r] = '\0';
    return strncmp(target, "/dev/spidev", 11) == 0;
}

static int is_gpiomem_fd(int fd) {
    if (fake_gpiomem_has(fd)) return 1;    /* substitute fd (see open hook) */
    char p[64], target[256];
    snprintf(p, sizeof(p), "/proc/self/fd/%d", fd);
    ssize_t r = readlink(p, target, sizeof(target) - 1);
    if (r <= 0) return 0;
    target[r] = '\0';
    return strcmp(target, "/dev/gpiomem") == 0;
}

/* -------------------------------------------------------------------------
 * gpiomem shadow: reflect tracked pin levels into GPLEV0 and force BUSY idle
 * ---------------------------------------------------------------------- */
static void refresh_gplev(void) {
    if (!gpio_shadow) return;
    uint32_t lev = pin_levels;
    if (busy_idle_high) lev |= (1u << PIN_BUSY);
    else                lev &= ~(1u << PIN_BUSY);
    gpio_shadow[GPLEV0] = lev;
}

/* Record a trapped GPIO register store in internal state ONLY.
 *
 * Critically, this must NOT write the shadow page: the page is PROT_READ while
 * centaur runs (that is how we trap the write), so a store from inside the
 * SIGSEGV handler would fault recursively and crash the process. We do not need
 * to persist centaur's writes anyway -- the real pins are not driven, and the
 * only register centaur READS is GPLEV0 (BUSY), which is seeded to idle once
 * (while the page is writable) and never changed, since RPi.GPIO never writes
 * GPLEV0. So here we just track the DC line (for SPI tagging) and pin levels.
 *
 * value is the 32-bit word centaur stored; word_idx is the register index. */
static void track_gpio_write(uint32_t word_idx, uint32_t value) {
    if (word_idx == GPSET0) {
        pin_levels |= value;                 /* set listed pins high */
        if (value & (1u << PIN_DC)) dc_state = 1;
    } else if (word_idx == GPCLR0) {
        pin_levels &= ~value;                /* drive listed pins low */
        if (value & (1u << PIN_DC)) dc_state = 0;
    }
    /* GPFSEL/GPPUD/etc: ignored -- no real pins, and not read back by centaur. */
}

/* -------------------------------------------------------------------------
 * SIGSEGV handler: trap writes to the (PROT_READ) shadow, decode the store,
 * apply it virtually, and step over the instruction.
 * ---------------------------------------------------------------------- */
static struct sigaction old_segv;

static void segv_handler(int sig, siginfo_t *si, void *uctx) {
    uintptr_t base = (uintptr_t)gpio_shadow;
    uintptr_t fault = (uintptr_t)si->si_addr;
    if (!gpio_shadow || fault < base || fault >= base + gpio_shadow_len) {
        /* Not ours: chain to the previous handler. */
        if (old_segv.sa_flags & SA_SIGINFO) {
            if (old_segv.sa_sigaction) old_segv.sa_sigaction(sig, si, uctx);
        } else if (old_segv.sa_handler == SIG_DFL || old_segv.sa_handler == SIG_IGN) {
            signal(sig, SIG_DFL);
            raise(sig);
        } else if (old_segv.sa_handler) {
            old_segv.sa_handler(sig);
        }
        return;
    }

    ucontext_t *uc = (ucontext_t *)uctx;
    unsigned long *regs = (unsigned long *)&uc->uc_mcontext.arm_r0;
    unsigned long pc    = uc->uc_mcontext.arm_pc;
    unsigned long cpsr  = uc->uc_mcontext.arm_cpsr;
    uint32_t word_idx = (uint32_t)((fault - base) / 4);

    dbg_faults++;
    if (cpsr & (1u << 5)) {
        /* Thumb state. Decoding T16/T32 stores is not implemented here; degrade
         * safely: unprotect the shadow so the re-executed store lands in RAM.
         * DC tracking is then lost (BUSY idle still holds, since RPi.GPIO never
         * writes GPLEV0), which is the on-hardware item to finish if _GPIO.so is
         * built Thumb. */
        dbg_thumb++;
        dbg_emit("[shim] fault THUMB -> degrade RW\n");
        mprotect((void *)base, gpio_shadow_len, PROT_READ | PROT_WRITE);
        return;
    }

    /* A32 single-data-transfer store of a word: bits[27:26]=01, L(20)=0,
     * B(22)=0. Source register Rt = bits[15:12]. The faulting address is
     * authoritative (si_addr), so only the stored value (regs[Rt]) is needed. */
    uint32_t instr = *(uint32_t *)pc;
    int is_str_word = ((instr >> 26) & 0x3) == 0x1 &&
                      !(instr & (1u << 20)) &&
                      !(instr & (1u << 22));
    if (is_str_word) {
        uint32_t rt = (instr >> 12) & 0xF;
        track_gpio_write(word_idx, (uint32_t)regs[rt]);
        dbg_decoded++;
        if (dbg_decoded <= 24) {
            char b[96];
            snprintf(b, sizeof(b),
                     "[shim] str word_idx=%u rt=%u val=0x%08x dc=%d\n",
                     word_idx, rt, (uint32_t)regs[rt], dc_state);
            dbg_emit(b);
        }
        uc->uc_mcontext.arm_pc = pc + 4;   /* skip the (now emulated) store */
        return;
    }

    /* Unrecognized store form: degrade safely rather than loop forever. */
    dbg_unknown++;
    {
        char b[64];
        snprintf(b, sizeof(b), "[shim] fault UNKNOWN instr=0x%08x -> degrade\n", instr);
        dbg_emit(b);
    }
    mprotect((void *)base, gpio_shadow_len, PROT_READ | PROT_WRITE);
}

static void install_segv_once(void) {
    static int installed = 0;
    if (installed) return;
    installed = 1;
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = segv_handler;
    sa.sa_flags = SA_SIGINFO;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, &old_segv);
}

/* -------------------------------------------------------------------------
 * Hooks
 * ---------------------------------------------------------------------- */
static void *make_gpio_shadow(size_t length) {
    size_t len = length < (size_t)page_size ? (size_t)page_size : length;
    void *p = real_mmap(NULL, len, PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) return MAP_FAILED;
    memset(p, 0, len);
    gpio_shadow = (volatile uint32_t *)p;
    gpio_shadow_len = len;
    pin_levels = 0;
    refresh_gplev();                 /* seed BUSY idle before centaur reads it */
    {
        char b[96];
        snprintf(b, sizeof(b), "[shim] gpiomem shadow created at %p len=%zu busy_idle_high=%d\n",
                 p, len, busy_idle_high);
        dbg_emit(b);
    }
    install_segv_once();
    /* Write-protect so DC writes trap; reads (incl. BUSY) pass through. */
    mprotect(p, len, PROT_READ);
    return p;
}

/* Only `mmap` is hooked: the bundled RPi/_GPIO.so imports mmap@GLIBC_2.4 (the
 * plain symbol, not mmap64), confirmed via readelf. Building this shim non-LFS
 * (see build.sh) keeps off_t 32-bit so the exported symbol is `mmap`, matching
 * that import. */
void *mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset) {
    if (!real_mmap) real_mmap = dlsym(RTLD_NEXT, "mmap");
    if (fd >= 0 && is_gpiomem_fd(fd) && !gpio_shadow) {
        void *p = make_gpio_shadow(length);
        if (p != MAP_FAILED) return p;
    }
    return real_mmap(addr, length, prot, flags, fd, offset);
}

/* -------------------------------------------------------------------------
 * /proc/cpuinfo virtualization (board-detection spoof)
 *
 * The bundled RPi/_GPIO.so identifies the SoC from the "Hardware : BCM<n>" line
 * of /proc/cpuinfo and raises "This module can only be run on a Raspberry Pi!"
 * at import -- before any GPIO access -- when it cannot. A 64-bit Raspberry Pi
 * OS kernel omits that line entirely (it keeps Revision/Serial/Model but drops
 * Hardware), so the module fails on EVERY arm64 host, from the Pi Zero 2 W to
 * the CM5 / Pi 5, regardless of whether a legacy /dev/gpiomem is present. That
 * missing line is what `spoof_cpuinfo` detects (see shim_init).
 *
 * Present a synthetic cpuinfo reporting a Pi 3B (BCM2837, revision a02082): a
 * board the bundled RPi.GPIO recognizes AND whose GPIO register layout matches
 * this shim's fixed GPSET0/GPCLR0/GPLEV0 word indices. The real peripheral base
 * is irrelevant because /dev/gpiomem is shadowed, so detection only needs to
 * succeed with a layout-compatible board. Every other path opens the real file.
 * Both fopen and fopen64 are hooked because a 32-bit LFS build of _GPIO.so links
 * the plain fopen or the 64-bit alias depending on its compile flags.
 * ---------------------------------------------------------------------- */
static const char FAKE_CPUINFO[] =
    "Hardware\t: BCM2835\n"
    "Revision\t: a02082\n"
    "Serial\t\t: 0000000000000000\n"
    "Model\t\t: Raspberry Pi 3 Model B Rev 1.2\n";

static int is_cpuinfo_path(const char *path) {
    return path && strcmp(path, "/proc/cpuinfo") == 0;
}

/* fmemopen over a static buffer: read-only, so the const cast is safe, and the
 * buffer outlives any FILE the caller fcloses (static storage). */
static FILE *open_fake_cpuinfo(void) {
    return fmemopen((void *)FAKE_CPUINFO, sizeof(FAKE_CPUINFO) - 1, "r");
}

/* Satisfy open("/dev/gpiomem") with a mappable substitute so RPi.GPIO does not
 * fall back to /dev/mem (see fake_gpiomem_fds). /dev/zero is always present,
 * world-rw and mmap-able; the shim replaces the mapping with a private shadow,
 * so the backing content is never used -- only a valid, mappable fd is needed. */
static int open_gpiomem_substitute(void) {
    if (!real_open) real_open = dlsym(RTLD_NEXT, "open");
    int fd = real_open("/dev/zero", O_RDWR);
    if (fd >= 0) fake_gpiomem_add(fd);
    return fd;
}

static int is_gpiomem_path(const char *path) {
    return path && strcmp(path, "/dev/gpiomem") == 0;
}

int open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = (mode_t)va_arg(ap, int); va_end(ap);
    }
    if (!real_open) real_open = dlsym(RTLD_NEXT, "open");
    if (spoof_gpiomem && is_gpiomem_path(path)) return open_gpiomem_substitute();
    return real_open(path, flags, mode);
}

int open64(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = (mode_t)va_arg(ap, int); va_end(ap);
    }
    if (!real_open64) real_open64 = dlsym(RTLD_NEXT, "open64");
    if (spoof_gpiomem && is_gpiomem_path(path)) return open_gpiomem_substitute();
    return real_open64(path, flags, mode);
}

FILE *fopen(const char *path, const char *mode) {
    if (!real_fopen) real_fopen = dlsym(RTLD_NEXT, "fopen");
    if (spoof_cpuinfo && is_cpuinfo_path(path)) return open_fake_cpuinfo();
    return real_fopen(path, mode);
}

FILE *fopen64(const char *path, const char *mode) {
    if (!real_fopen64) real_fopen64 = dlsym(RTLD_NEXT, "fopen64");
    if (spoof_cpuinfo && is_cpuinfo_path(path)) return open_fake_cpuinfo();
    return real_fopen64(path, mode);
}

ssize_t write(int fd, const void *buf, size_t n) {
    if (!real_write) real_write = dlsym(RTLD_NEXT, "write");
    if (n && is_spidev_fd(fd)) {
        forward_xfer((const unsigned char *)buf, (uint32_t)n);
        return (ssize_t)n;            /* swallow: real panel never sees it */
    }
    return real_write(fd, buf, n);
}

ssize_t writev(int fd, const struct iovec *iov, int cnt) {
    if (!real_writev) real_writev = dlsym(RTLD_NEXT, "writev");
    if (iov && cnt > 0 && is_spidev_fd(fd)) {
        ssize_t total = 0;
        for (int i = 0; i < cnt; i++) {
            if (iov[i].iov_len) {
                forward_xfer((const unsigned char *)iov[i].iov_base,
                             (uint32_t)iov[i].iov_len);
                total += (ssize_t)iov[i].iov_len;
            }
        }
        return total;                 /* swallow */
    }
    return real_writev(fd, iov, cnt);
}

int ioctl(int fd, unsigned long request, ...) {
    if (!real_ioctl) real_ioctl = dlsym(RTLD_NEXT, "ioctl");
    va_list ap;
    va_start(ap, request);
    void *argp = va_arg(ap, void *);
    va_end(ap);

    unsigned char type = (request >> _IOC_TYPESHIFT) & _IOC_TYPEMASK;
    unsigned char nr   = (request >> _IOC_NRSHIFT) & _IOC_NRMASK;

    /* SPI_IOC_MESSAGE(N): nr==0, magic 'k'. Forward tx buffers, swallow the
     * transfer (do not touch the bus), and report the byte count. Other spidev
     * config ioctls (mode/speed/bits) pass through harmlessly. */
    if (argp && type == SPI_IOC_MAGIC && nr == 0 && is_spidev_fd(fd)) {
        size_t total_len = _IOC_SIZE(request);
        size_t n = total_len / sizeof(struct spi_ioc_transfer);
        struct spi_ioc_transfer *t = (struct spi_ioc_transfer *)argp;
        uint32_t bytes = 0;
        for (size_t i = 0; i < n; i++) {
            if (t[i].tx_buf && t[i].len) {
                forward_xfer((const unsigned char *)(uintptr_t)t[i].tx_buf, t[i].len);
                bytes += t[i].len;
            }
        }
        return (int)bytes;
    }
    return real_ioctl(fd, request, argp);
}

/* Decide whether the synthetic cpuinfo is needed by reading the REAL
 * /proc/cpuinfo and checking for a line beginning with "Hardware". The bundled
 * (old, armhf) RPi.GPIO requires that line to identify the SoC; a 64-bit
 * Raspberry Pi OS kernel omits it (keeping Revision/Serial/Model), which is why
 * the module fails on every arm64 host. A 32-bit kernel always includes it, so
 * native boards read as present and are left untouched -- decoupling this from
 * the /dev/gpiomem (RP1) question, which the Zero 2 W would otherwise fail: it
 * has a legacy /dev/gpiomem yet a Hardware-less 64-bit cpuinfo.
 *
 * Uses the resolved real_open (set before this runs) plus raw read/close, none
 * of which re-enter our hooks, so it is safe to call from the constructor. If
 * the file cannot be read, returns 0 (assume native) so a probe failure never
 * spoofs cpuinfo on a host that does not need it. */
static int real_cpuinfo_lacks_hardware_line(void) {
    int fd = real_open ? real_open("/proc/cpuinfo", O_RDONLY)
                       : open("/proc/cpuinfo", O_RDONLY);
    if (fd < 0) return 0;
    char buf[16384];
    size_t total = 0;
    ssize_t r;
    while (total < sizeof(buf) - 1 &&
           (r = read(fd, buf + total, sizeof(buf) - 1 - total)) > 0) {
        total += (size_t)r;
    }
    close(fd);
    buf[total] = '\0';
    if (strncmp(buf, "Hardware", 8) == 0) return 0;   /* Hardware is the first line */
    return strstr(buf, "\nHardware") == NULL;
}

__attribute__((constructor))
static void shim_init(void) {
    for (int i = 0; i < MAX_FAKE_GPIOMEM_FDS; i++) fake_gpiomem_fds[i] = -1;

    /* Resolve the real entry points first: real_cpuinfo_lacks_hardware_line()
     * below reads /proc/cpuinfo through real_open. */
    real_write   = dlsym(RTLD_NEXT, "write");
    real_writev  = dlsym(RTLD_NEXT, "writev");
    real_ioctl   = dlsym(RTLD_NEXT, "ioctl");
    real_mmap    = dlsym(RTLD_NEXT, "mmap");
    real_fopen   = dlsym(RTLD_NEXT, "fopen");
    real_fopen64 = dlsym(RTLD_NEXT, "fopen64");
    real_open    = dlsym(RTLD_NEXT, "open");
    real_open64  = dlsym(RTLD_NEXT, "open64");

    /* Two independent gates (see the flag declarations and the top-of-file note).
     * cpuinfo spoof: needed on any 64-bit kernel (missing "Hardware" line).
     * gpiomem substitute: needed only where the legacy device is absent (RP1). */
    spoof_cpuinfo = real_cpuinfo_lacks_hardware_line();
    spoof_gpiomem = (access("/dev/gpiomem", F_OK) != 0);

    page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0) page_size = 4096;
    const char *bih = getenv("UC_CENTAUR_BUSY_IDLE_HIGH");
    if (bih && *bih) busy_idle_high = atoi(bih) != 0;
    const char *dbg = getenv("UC_CENTAUR_SHIM_DEBUG");
    if (dbg && *dbg) {
        dbg_fd = open(dbg, O_WRONLY | O_CREAT | O_APPEND, 0644);
        char b[96];
        snprintf(b, sizeof(b), "[shim] loaded (spoof_cpuinfo=%d spoof_gpiomem=%d)\n",
                 spoof_cpuinfo, spoof_gpiomem);
        dbg_emit(b);
    }
}
