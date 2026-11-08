*** Comments ***
Firmware tests on Renode's emulated STM32F4 (docs/FIRMWARE.md, Phase 4). Emulated, not a board.

    scripts/edge-dev.sh bash -c 'cd /src && firmware/renode/tests/run.sh'

Unlike firmware/tests/renode_test.py, which lets the MCU run free against wall-clock reads,
every test here advances the emulation explicitly (`emulation RunFor`) and reads a file-backed
UART afterwards. Counts and rates are therefore exact in emulated time and identical on every
host: a slow CI runner makes the suite take longer, never fail.

Fault injection writes the firmware's fault_ctl words (board.h, 0x2001FE00): +0 stall a task,
+4 comms delay in ms, +8 stop sensors (bit 1 imu, bit 2 range). The boot record (boot_record.h,
0x2001FF00) is read straight from emulated RAM: +4 boot_count.

*** Settings ***
Library           OfpKeywords.py
Suite Teardown    Write Firmware Metrics

*** Variables ***
${ELF}            ${CURDIR}/../../build/openfleet-fw.elf
${REPL}           ${CURDIR}/../openfleet-f4.repl
${MACHINE}        ${CURDIR}/../openfleet-machine.resc
${FAULT_CTL}      0x2001FE00
${BOOT_RECORD}    0x2001FF00
${TASK_SENSOR}    1
${IMU}            1
${RANGE}          2
${MCU_HZ}         16000000

*** Keywords ***
Create MCU
    [Arguments]    ${seed}=42
    Execute Command    emulation SetSeed ${seed}
    Execute Command    $bin=@${ELF}; $repl=@${REPL}
    Execute Command    include @${MACHINE}
    ${capture}=    New Uart Capture
    Execute Command    sysbus.usart2 CreateFileBackend @${capture} true

Run For
    [Arguments]    ${seconds}
    Execute Command    emulation RunFor "${seconds}"
    ${frames}=    Read New Frames
    RETURN    ${frames}

Poke Fault
    [Arguments]    ${offset}    ${value}
    ${addr}=    Evaluate    hex(${FAULT_CTL} + ${offset})
    Execute Command    sysbus WriteDoubleWord ${addr} ${value}

Read Boot Record Word
    [Arguments]    ${offset}
    ${addr}=    Evaluate    hex(${BOOT_RECORD} + ${offset})
    ${out}=    Execute Command    sysbus ReadDoubleWord ${addr}
    ${value}=    Evaluate    int("""${out}""".strip(), 16)
    RETURN    ${value}

Executed Instructions
    ${out}=    Execute Command    sysbus.cpu ExecutedInstructions
    ${value}=    Evaluate    int("""${out}""".strip(), 16)
    RETURN    ${value}

Write Firmware Metrics
    ${path}=    Write Metrics    ${OUTPUT_DIR}/firmware-metrics.json
    Log To Console    metrics: ${path}

*** Test Cases ***
Boots With A Power-On HELLO And Describes Itself
    Create MCU
    ${frames}=    Run For    3
    ${reasons}=    Reset Reasons    ${frames}
    Should Be Equal    ${reasons}    ${{ ['power-on'] }}
    ${fw}=    Firmware Version    ${frames}
    Should Be Equal    ${fw}    openfleet-fw 0.1
    ${names}=    Described Names    ${frames}
    Should Be Equal As Numbers    ${names}[imu]    50
    Should Be Equal As Numbers    ${names}[range]    10
    Dictionary Should Contain Key    ${names}    sensor
    Dictionary Should Contain Key    ${names}    comms
    Dictionary Should Contain Key    ${names}    health
    Seq Should Be Contiguous    ${frames}
    Bad Frames Should Be Zero
    ${boots}=    Read Boot Record Word    4
    Should Be Equal As Integers    ${boots}    1

Samples At Exactly 50 And 10 Hz In Emulated Time
    Create MCU
    Run For    2
    ${frames}=    Run For    10
    ${imu}=    Count Sensor Readings    ${frames}    ${IMU}
    ${range}=    Count Sensor Readings    ${frames}    ${RANGE}
    # 10 s of emulated time; +-1 for where the window edges fall between two readings.
    Should Be True    499 <= ${imu} <= 501    imu: ${imu} readings in 10 s
    Should Be True    99 <= ${range} <= 101    range: ${range} readings in 10 s
    ${states}=    Task States    ${frames}    ${TASK_SENSOR}
    Should Not Be Empty    ${states}
    Should Be True    all(s == 0 for s in ${states})    sensor task not always RUNNING: ${states}
    ${stack}=    Lowest Free Stack    ${frames}
    Should Be True    ${stack} >= 16    lowest free stack ${stack} words
    Record Metric    lowest_free_stack_words    ${stack}    words
    ...    lowest stack headroom any task reported in 12 s
    Seq Should Be Contiguous    ${frames}

Same Seed Gives The Same Bytes
    [Documentation]    The emulation is deterministic: one seed, one UART byte stream.
    Create MCU    seed=7
    Run For    3
    ${first}=    Capture Digest
    Execute Command    Clear
    Create MCU    seed=7
    Run For    3
    ${second}=    Capture Digest
    Should Be Equal    ${first}    ${second}

A Stalled Task Makes The Watchdog Reset The MCU
    Create MCU
    ${before}=    Run For    2
    ${old_boot}=    Boot Ids    ${before}
    Poke Fault    0    ${TASK_SENSOR}
    ${frames}=    Run For    4
    Logs Should Contain    ${frames}    watchdog: task sensor stopped checking in
    ${reasons}=    Reset Reasons    ${frames}
    Should Be Equal    ${reasons}    ${{ ['watchdog'] }}
    Logs Should Contain    ${frames}    previous boot ended by the watchdog: task sensor
    ${boots}=    Read Boot Record Word    4
    Should Be Equal As Integers    ${boots}    2
    ${ids}=    Boot Ids    ${frames}
    ${expected}=    Evaluate    (${old_boot}[0] + 1) & 0xFFFF
    Should Be Equal As Integers    ${ids}[-1]    ${expected}
    # The fault was cleared by the reset: the new boot samples at full rate again.
    ${after}=    Run For    2
    ${imu}=    Count Sensor Readings    ${after}    ${IMU}
    Should Be True    99 <= ${imu} <= 101    imu after the reset: ${imu} in 2 s

Watchdog Reset Recovery Time In Emulated Time
    [Documentation]    From the stall to the first reading of the next boot, stepping the
    ...    emulation in 50 ms slices: exact in emulated time to one slice.
    Create MCU
    ${before}=    Run For    2
    ${old_boot}=    Boot Ids    ${before}
    Poke Fault    0    ${TASK_SENSOR}
    ${recovery_ms}=    Set Variable    ${None}
    FOR    ${step}    IN RANGE    1    101
        ${frames}=    Run For    0.05
        ${new}=    New Boot Sensor Readings    ${frames}    ${old_boot}[0]
        IF    ${new} > 0
            ${recovery_ms}=    Evaluate    ${step} * 50
            BREAK
        END
    END
    Should Not Be Equal    ${recovery_ms}    ${None}    no new boot within 5 s of the stall
    # Watchdog task notices (supervisor deadline) + IWDG timeout + boot to the first reading.
    Should Be True    ${recovery_ms} < 2500    stall to first new reading: ${recovery_ms} ms
    Log To Console    stall -> first reading of the next boot: ${recovery_ms} ms (emulated)
    Record Metric    watchdog_recovery_ms    ${recovery_ms}    ms
    ...    stalled task -> watchdog reset -> first SENSOR of the next boot (emulated time, 50 ms steps)

A Slow Link Drops Readings But Never Resets
    Create MCU
    Run For    2
    Poke Fault    4    60
    ${frames}=    Run For    6
    ${reasons}=    Reset Reasons    ${frames}
    Should Be Empty    ${reasons}
    Logs Should Contain    ${frames}    readings dropped
    ${states}=    Task States    ${frames}    ${TASK_SENSOR}
    Should Contain    ${states}    ${1}
    Poke Fault    4    0
    Run For    2
    ${frames}=    Run For    4
    ${imu}=    Count Sensor Readings    ${frames}    ${IMU}
    Should Be True    199 <= ${imu} <= 201    imu after the link recovered: ${imu} in 4 s

Stopped Sensors Go Quiet While The Tasks Stay Healthy
    Create MCU
    Run For    2
    Poke Fault    8    ${{ 1 << ${RANGE} }}
    ${frames}=    Run For    4
    ${range}=    Count Sensor Readings    ${frames}    ${RANGE}
    ${imu}=    Count Sensor Readings    ${frames}    ${IMU}
    # A reading already queued when the fault lands may still go out.
    Should Be True    ${range} <= 1    range kept reporting: ${range}
    Should Be True    199 <= ${imu} <= 201    imu: ${imu} in 4 s
    ${reasons}=    Reset Reasons    ${frames}
    Should Be Empty    ${reasons}

Firmware CPU Cost Per Second Of Device Time
    [Documentation]    Executed instructions per emulated second, at steady state. Renode is
    ...    not cycle-accurate, so this is a relative cost figure, not a CPU load.
    Create MCU
    Run For    2
    ${i0}=    Executed Instructions
    ${frames}=    Run For    10
    ${i1}=    Executed Instructions
    ${per_s}=    Evaluate    (${i1} - ${i0}) / 10
    ${count}=    Get Length    ${frames}
    ${per_frame}=    Evaluate    (${i1} - ${i0}) / ${count}
    ${share}=    Evaluate    100 * ${per_s} / ${MCU_HZ}
    Log To Console    ${per_s} instructions/s, ${per_frame} per frame sent, ${share}% of ${MCU_HZ} Hz at 1 IPC
    # A budget, not a measurement: the work is ~60 readings/s, so even 10 % would be a bug.
    Should Be True    ${share} < 10    ${share}%
    Record Metric    instructions_per_s    ${per_s}    instr/s
    ...    instructions executed per second of device time (WFI idle excluded)
    Record Metric    instructions_per_frame    ${per_frame}    instr/frame
    ...    all firmware work divided by frames sent (sampling, queueing, encoding, UART)
    Record Metric    duty_share_pct    ${share}    %
    ...    instructions/s as a share of 16 MHz, assuming 1 instruction per cycle
