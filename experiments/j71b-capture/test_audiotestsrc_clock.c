/* Exercise the unchanged pinned SPA source with an in-memory host clock.
 * No PipeWire context, daemon, socket, thread, device or OS timer is created.
 * The source implementation comes from the verified upstream include path.
 */
#include <assert.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <spa/utils/json-pod.h>
#include "audiotestsrc.c"

struct test_host {
    struct spa_loop loop;
    struct spa_system system;
    struct spa_log log;
    struct spa_source *source;
    bool armed;
    unsigned ready, starvation;
};

static int fake_timer_create(void *data, int clockid, int flags) { return 99; }
static int timer_close(void *data, int fd) { assert(fd == 99); return 0; }
static int timer_read(void *data, int fd, uint64_t *expirations)
{
    assert(fd == 99);
    *expirations = 1;
    return 0;
}
static int timer_set(void *data, int fd, int flags,
                     const struct itimerspec *value, struct itimerspec *old)
{
    struct test_host *host = data;
    assert(fd == 99);
    host->armed = value->it_value.tv_sec || value->it_value.tv_nsec;
    return 0;
}
static int add_source(void *data, struct spa_source *source)
{
    struct test_host *host = data;
    host->source = source;
    return 0;
}
static int remove_source(void *data, struct spa_source *source)
{
    struct test_host *host = data;
    assert(host->source == source);
    host->source = NULL;
    return 0;
}
static int locked(void *data, spa_invoke_func_t func, uint32_t seq,
                  const void *payload, size_t size, void *user_data)
{
    struct test_host *host = data;
    return func(&host->loop, false, seq, payload, size, user_data);
}
static void log_message(void *data, enum spa_log_level level,
                        const char *file, int line, const char *func,
                        const char *format, va_list args)
{
    struct test_host *host = data;
    char message[256];
    vsnprintf(message, sizeof(message), format, args);
    assert(level == SPA_LOG_LEVEL_ERROR);
    assert(strstr(message, "out of buffers"));
    host->starvation++;
    fprintf(stderr, "expected source diagnostic: %s:%d %s: %s\n",
            file, line, func, message);
}
static void log_variadic(void *data, enum spa_log_level level,
                         const char *file, int line, const char *func,
                         const char *format, ...)
{
    va_list args;
    va_start(args, format);
    log_message(data, level, file, line, func, format, args);
    va_end(args);
}
static int ready(void *data, int status)
{
    struct test_host *host = data;
    assert(status == SPA_STATUS_HAVE_DATA);
    host->ready++;
    return 0;
}

static void exercise_json_frequency(void)
{
    /* Exactly the JSON-to-POD path used by pw-cli set-param. The adapter
     * forwards this POD, but the pinned type table says Int while the source
     * parses only Float. Even a decimal JSON literal does not fix the type.
     * No initialized SPA host, clock, socket, or timer is needed here. */
    const char *cases[] = {"{ frequency: 1000, volume: 0.1 }",
                          "{ frequency: 1000.0, volume: 0.1 }"};
    const struct spa_type_info *type = spa_debug_type_find_short(spa_type_param, "Props");
    assert(type != NULL);
    for (unsigned i = 0; i < SPA_N_ELEMENTS(cases); i++) {
        struct impl source = {0};
        uint8_t bytes[1024];
        struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(bytes, sizeof(bytes));
        reset_props(&source.props);
        assert(source.props.freq == 440.0f);
        assert(spa_json_to_pod(&builder, 0, type, cases[i], strlen(cases[i])) == 0);
        const struct spa_pod *pod = spa_pod_builder_deref(&builder, 0);
        const struct spa_pod_prop *frequency = spa_pod_find_prop(pod, NULL, SPA_PROP_frequency);
        assert(frequency != NULL && frequency->value.type == SPA_TYPE_Int);
        assert(impl_node_set_param(&source, SPA_PARAM_Props, 0, pod) == 0);
        assert(source.props.freq == 440.0f && source.props.volume == 0.1f);

        /* Real unchanged generator samples confirm the ignored override is
         * still a 440 Hz sine, not merely a stale property enumeration. */
        float samples[12000 * 2];
        double sine = 0.0, cosine = 0.0, wrong_sine = 0.0, wrong_cosine = 0.0;
        source.port.current_format.info.raw.rate = 48000;
        source.port.current_format.info.raw.channels = 2;
        audio_test_src_create_sine_float(&source, samples, 12000);
        for (unsigned n = 0; n < 12000; n++) {
            double phase = 2.0 * M_PI * n / 48000;
            assert(samples[n * 2] == samples[n * 2 + 1]);
            sine += samples[n * 2] * sin(phase * 440);
            cosine += samples[n * 2] * cos(phase * 440);
            wrong_sine += samples[n * 2] * sin(phase * 1000);
            wrong_cosine += samples[n * 2] * cos(phase * 1000);
        }
        assert(fabs(2.0 * hypot(sine, cosine) / 12000 - 0.1) < 0.00001);
        assert(2.0 * hypot(wrong_sine, wrong_cosine) / 12000 < 0.00001);

        /* A correctly typed direct SPA update does change the generator.
         * Keep the CI fixture simple instead: use and verify pinned 440 Hz. */
        spa_pod_builder_init(&builder, bytes, sizeof(bytes));
        pod = spa_pod_builder_add_object(&builder, SPA_TYPE_OBJECT_Props, SPA_PARAM_Props,
                                         SPA_PROP_frequency, SPA_POD_Float(1000.0f));
        assert(impl_node_set_param(&source, SPA_PARAM_Props, 0, pod) == 0);
        assert(source.props.freq == 1000.0f);
    }
    puts("PASS: pw-cli JSON frequency is Int and ignored by Float-only audiotestsrc; pinned 440 Hz PCM verified");
}

static void exercise(bool follower)
{
    const struct spa_loop_methods loop_methods = {
        .version = SPA_VERSION_LOOP_METHODS,
        .add_source = add_source, .remove_source = remove_source, .locked = locked,
    };
    const struct spa_system_methods system_methods = {
        .version = SPA_VERSION_SYSTEM_METHODS,
        .timerfd_create = fake_timer_create, .timerfd_settime = timer_set,
        .timerfd_read = timer_read, .close = timer_close,
    };
    const struct spa_log_methods log_methods = {
        .version = 0, .log = log_variadic, .logv = log_message,
    };
    const struct spa_node_callbacks callbacks = { .ready = ready };
    struct test_host host = {0};
    struct impl source = {0};
    struct spa_support support[] = {
        {SPA_TYPE_INTERFACE_DataLoop, &host.loop},
        {SPA_TYPE_INTERFACE_DataSystem, &host.system},
        {SPA_TYPE_INTERFACE_Log, &host.log},
    };
    struct spa_io_clock clock = {.id = 10};
    struct spa_io_position position = { .clock = {
        .id = follower ? 11 : 10, .duration = 128, .target_duration = 128,
        .rate = {1, 48000}, .target_rate = {1, 48000},
    }};
    struct spa_io_buffers io = SPA_IO_BUFFERS_INIT;
    float samples[2][256] = {{0}};
    struct spa_chunk chunks[2] = {{0}};
    struct spa_data data[2];
    struct spa_buffer buffers[2];
    struct spa_buffer *buffer_ptrs[] = {&buffers[0], &buffers[1]};
    uint8_t pod_buffer[1024];
    struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(pod_buffer, sizeof(pod_buffer));
    struct spa_audio_info_raw format = {
        .format = SPA_AUDIO_FORMAT_F32, .rate = 48000, .channels = 2,
        .position = {SPA_AUDIO_CHANNEL_FL, SPA_AUDIO_CHANNEL_FR},
    };
    const struct spa_command start = SPA_NODE_COMMAND_INIT(SPA_NODE_COMMAND_Start);
    const struct spa_command pause = SPA_NODE_COMMAND_INIT(SPA_NODE_COMMAND_Pause);

    host.loop.iface = SPA_INTERFACE_INIT(SPA_TYPE_INTERFACE_DataLoop, 0, &loop_methods, &host);
    host.system.iface = SPA_INTERFACE_INIT(SPA_TYPE_INTERFACE_DataSystem, 0, &system_methods, &host);
    host.log.iface = SPA_INTERFACE_INIT(SPA_TYPE_INTERFACE_Log, 0, &log_methods, &host);
    host.log.level = SPA_LOG_LEVEL_ERROR;
    assert(impl_init(&spa_audiotestsrc_factory, &source.handle, NULL,
                     support, SPA_N_ELEMENTS(support)) == 0);
    struct spa_node *node = &source.node;
    assert(source.props.live);  /* Test the same default live mode as CI. */
    assert(spa_node_set_callbacks(node, &callbacks, &host) == 0);
    assert(spa_node_set_io(node, SPA_IO_Clock, &clock, sizeof(clock)) == 0);
    assert(spa_node_set_io(node, SPA_IO_Position, &position, sizeof(position)) == 0);
    assert(source.following == follower);
    assert(spa_node_port_set_param(node, SPA_DIRECTION_OUTPUT, 0, SPA_PARAM_Format, 0,
            spa_format_audio_raw_build(&builder, SPA_PARAM_Format, &format)) == 0);
    for (unsigned i = 0; i < 2; i++) {
        data[i] = (struct spa_data) { .type = SPA_DATA_MemPtr, .data = samples[i],
            .maxsize = sizeof(samples[i]), .chunk = &chunks[i] };
        buffers[i] = (struct spa_buffer) { .n_datas = 1, .datas = &data[i] };
    }
    assert(spa_node_port_use_buffers(node, SPA_DIRECTION_OUTPUT, 0, 0, buffer_ptrs, 2) == 0);
    assert(spa_node_port_set_io(node, SPA_DIRECTION_OUTPUT, 0, SPA_IO_Buffers, &io, sizeof(io)) == 0);
    assert(spa_node_send_command(node, &start) == 0);
    assert(host.armed == !follower);

    /* Hold both buffers, then request one more quantum to reproduce starvation. */
    for (unsigned i = 0; i < 3; i++) {
        io.status = SPA_STATUS_NEED_DATA;
        io.buffer_id = SPA_ID_INVALID;
        if (follower) {
            assert(spa_node_process(node) == (i < 2 ? SPA_STATUS_HAVE_DATA : -EPIPE));
        } else {
            assert(host.armed);
            host.source->func(host.source);
        }
    }
    assert(source.started && !host.armed && host.starvation == 1);
    assert(spa_node_port_reuse_buffer(node, 0, 0) == 0);
    assert(spa_node_port_reuse_buffer(node, 0, 1) == 0);
    assert(!host.armed);
    io.status = SPA_STATUS_NEED_DATA;
    io.buffer_id = SPA_ID_INVALID;
    if (!follower) {
        assert(spa_node_process(node) == SPA_STATUS_OK);
        assert(source.started && !host.armed && host.ready == 2);
        puts("PASS: live self-driver starvation disarms timer; returned buffers do not rearm it");
    } else {
        for (unsigned cycle = 0; cycle < 3; cycle++) {
            for (unsigned i = 0; i < 64; i++) {
                io.status = SPA_STATUS_NEED_DATA;
                assert(spa_node_process(node) == SPA_STATUS_HAVE_DATA);
                assert(chunks[io.buffer_id].size == 128 * 2 * sizeof(float));
                float peak = 0;
                for (unsigned j = 0; j < 256; j++) {
                    float value = samples[io.buffer_id][j];
                    assert(isfinite(value));
                    peak = fmaxf(peak, fabsf(value));
                }
                assert(peak > 0.1f && !host.armed && host.ready == 0);
            }
            assert(spa_node_send_command(node, &pause) == 0);
            assert(!source.started && !host.armed);
            if (cycle < 2) {
                assert(spa_node_send_command(node, &start) == 0);
                assert(source.started && !host.armed);
            }
        }
        puts("PASS: externally clocked live follower recovers after buffer return; 192 finite/nonzero quanta across three activations, timer never armed");
    }
    assert(spa_node_send_command(node, &pause) == 0);
    assert(impl_clear(&source.handle) == 0);
    assert(host.source == NULL);
}

int main(void)
{
    exercise_json_frequency();
    exercise(false);
    exercise(true);
    return 0;
}
