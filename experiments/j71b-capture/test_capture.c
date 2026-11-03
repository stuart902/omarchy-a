/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <stdio.h>
#include "capture.c"

int main(void)
{
    float left[4096], right[4096], gain = 24.0f, overload, invalid;
    struct capture *c;
    const LADSPA_Descriptor *d = ladspa_descriptor(0);
    unsigned int i;
    assert(d && !ladspa_descriptor(1));
    assert(!instantiate(d, 0) && !instantiate(d, 7999));
    assert(!instantiate(d, 192001));
    c = instantiate(d, 48000);
    assert(c);
    connect_port(c, IN_L, left);
    connect_port(c, IN_R, right);
    connect_port(c, OUT_L, left); /* Explicitly exercise in-place buffers. */
    connect_port(c, OUT_R, right);
    connect_port(c, GAIN_DB, &gain);
    connect_port(c, OVERLOAD, &overload);
    connect_port(c, INVALID, &invalid);
    connect_port(c, PORTS + 2, NULL); /* Out-of-range port cannot write memory. */
    for (i = 0; i < 4096; i++) left[i] = right[i] = -.06f;
    activate(c);
    run(c, 4096);
    for (i = 0; i < 4096; i++) assert(fabs(left[i]) < 1e-8);
    assert(overload == 0 && invalid == 0);

    for (i = 0; i < 4096; i++) left[i] = right[i] = (i & 1) ? 1 : -1;
    run(c, 4096);
    assert(overload > 0);
    for (i = 0; i < 4096; i++) assert(fabs(left[i]) <= .891251);

    left[0] = NAN; left[1] = INFINITY; left[2] = -INFINITY;
    right[0] = right[1] = right[2] = .05f;
    run(c, 3);
    assert(invalid == 3);
    assert(left[0] == 0 && left[1] == 0 && left[2] == 0);
    assert(isfinite(right[0]) && isfinite(right[1]) && isfinite(right[2]));
    activate(c);
    run(c, 0);
    assert(overload == 0 && invalid == 0);

    /* Missing controls are zero gain, and missing audio ports are harmless. */
    connect_port(c, GAIN_DB, NULL);
    connect_port(c, IN_L, NULL);
    connect_port(c, OUT_L, NULL);
    run(c, 3);
    connect_port(c, GAIN_DB, &gain);
    gain = NAN;
    run(c, 3);
    assert(isfinite(c->gain));
    free(c);
    puts("C lifecycle, in-place, overload, invalid-input and port tests passed");
    return 0;
}
