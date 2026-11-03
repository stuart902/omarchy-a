/* SPDX-License-Identifier: MIT
 * Experimental capture conditioning, not installed by the distribution.
 */
#include <ladspa.h>
#include <math.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

#define CHANNELS 2
#define PI 3.14159265358979323846
#define MAX_GAIN_DB 24.0
/* Explicit emergency digital bound, not speaker protection or an AGC. */
#define OUTPUT_BOUND 0.8912509381337456 /* -1 dBFS */
#define COUNTER_MAX 16777216.0f

enum { IN_L, IN_R, OUT_L, OUT_R, GAIN_DB, OVERLOAD, INVALID, PORTS };
struct channel {
    double z1, z2;
    unsigned long age;
    bool initialized;
};
struct capture {
    LADSPA_Data *port[PORTS];
    struct channel channel[CHANNELS];
    double b0, b1, b2, a1, a2, gain, smoothing;
    unsigned long fade_frames;
    float overload, invalid;
};

static LADSPA_Handle instantiate(const LADSPA_Descriptor *desc, unsigned long rate)
{
    struct capture *c;
    double omega, cs, alpha, denominator;
    (void)desc;
    if (rate < 8000 || rate > 192000)
        return NULL;
    c = calloc(1, sizeof(*c));
    if (!c)
        return NULL;
    /* Second-order Butterworth high-pass, 80 Hz. Coefficients never change
     * during processing. Double state avoids slow single-precision DC drift.
     */
    omega = 2.0 * PI * 80.0 / rate;
    cs = cos(omega);
    alpha = sin(omega) / sqrt(2.0);
    denominator = 1.0 + alpha;
    c->b0 = (1.0 + cs) / (2.0 * denominator);
    c->b1 = -2.0 * c->b0;
    c->b2 = c->b0;
    c->a1 = -2.0 * cs / denominator;
    c->a2 = (1.0 - alpha) / denominator;
    c->fade_frames = (rate + 49) / 50; /* 20 ms, rounded up */
    c->smoothing = -expm1(-1.0 / (0.020 * rate));
    c->gain = 1.0;
    return c;
}

static void connect_port(LADSPA_Handle handle, unsigned long port, LADSPA_Data *data)
{
    struct capture *c = handle;
    if (port < PORTS)
        c->port[port] = data;
}

static void activate(LADSPA_Handle handle)
{
    struct capture *c = handle;
    memset(c->channel, 0, sizeof(c->channel));
    c->gain = 1.0;
    c->overload = c->invalid = 0.0f;
}

static void count(float *counter)
{
    if (*counter < COUNTER_MAX)
        *counter += 1.0f;
}

static void run(LADSPA_Handle handle, unsigned long frames)
{
    struct capture *c = handle;
    double db = c->port[GAIN_DB] ? *c->port[GAIN_DB] : 0.0;
    double target;
    unsigned long n;
    unsigned int ch;
    if (!isfinite(db))
        db = 0.0;
    db = fmax(0.0, fmin(db, MAX_GAIN_DB));
    target = pow(10.0, db / 20.0);
    for (n = 0; n < frames; n++) {
        /* Read both channels before writing either, including in-place use. */
        double input[CHANNELS] = {
            c->port[IN_L] ? c->port[IN_L][n] : 0.0,
            c->port[IN_R] ? c->port[IN_R][n] : 0.0,
        };
        c->gain += c->smoothing * (target - c->gain);
        for (ch = 0; ch < CHANNELS; ch++) {
            struct channel *s = &c->channel[ch];
            double x = input[ch], y = 0.0;
            if (!isfinite(x)) {
                count(&c->invalid);
                memset(s, 0, sizeof(*s));
                goto output;
            }
            if (!s->initialized) {
                /* Initial transport silence must not consume initialization.
                 * This is only startup handling, not a silence detector.
                 */
                if (x == 0.0)
                    goto output;
                s->z1 = -c->b0 * x;
                s->z2 = c->b2 * x;
                s->initialized = true;
            }
            y = c->b0 * x + s->z1;
            s->z1 = c->b1 * x - c->a1 * y + s->z2;
            s->z2 = c->b2 * x - c->a2 * y;
            /* Avoid denormal tails without biasing the audible signal. */
            if (fabs(s->z1) < 1e-30) s->z1 = 0.0;
            if (fabs(s->z2) < 1e-30) s->z2 = 0.0;
            if (s->age < c->fade_frames) {
                y *= (double)s->age / c->fade_frames;
                s->age++;
            }
            y *= c->gain;
            if (y > OUTPUT_BOUND || y < -OUTPUT_BOUND) {
                count(&c->overload);
                y = fmax(-OUTPUT_BOUND, fmin(y, OUTPUT_BOUND));
            }
output:
            if (c->port[OUT_L + ch])
                c->port[OUT_L + ch][n] = (float)y;
        }
    }
    if (c->port[OVERLOAD]) *c->port[OVERLOAD] = c->overload;
    if (c->port[INVALID]) *c->port[INVALID] = c->invalid;
}

static const LADSPA_PortDescriptor ports[PORTS] = {
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_CONTROL,
};
static const char *const names[PORTS] = {
    "Input L", "Input R", "Output L", "Output R", "Gain (dB)",
    "Overload samples", "Invalid samples"
};
static const LADSPA_PortRangeHint hints[PORTS] = {
    {0, 0, 0}, {0, 0, 0}, {0, 0, 0}, {0, 0, 0},
    {LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE |
     LADSPA_HINT_DEFAULT_0, 0, MAX_GAIN_DB},
    {LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE |
     LADSPA_HINT_INTEGER, 0, COUNTER_MAX},
    {LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE |
     LADSPA_HINT_INTEGER, 0, COUNTER_MAX},
};
static const LADSPA_Descriptor descriptor = {
    /* Private experiment: no globally registered LADSPA identifier claimed. */
    .UniqueID = 0,
    .Label = "j71b_capture_experiment",
    .Properties = 0,
    .Name = "J71b experimental capture conditioner",
    .Maker = "Omarchy-A contributors",
    .Copyright = "MIT",
    .PortCount = PORTS,
    .PortDescriptors = ports,
    .PortNames = names,
    .PortRangeHints = hints,
    .instantiate = instantiate,
    .connect_port = connect_port,
    .activate = activate,
    .run = run,
    .cleanup = free,
};
const LADSPA_Descriptor *ladspa_descriptor(unsigned long index)
{
    return index == 0 ? &descriptor : NULL;
}
