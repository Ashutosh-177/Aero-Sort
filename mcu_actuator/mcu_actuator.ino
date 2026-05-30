#include <Arduino.h>

const uint8_t VALVE_1_PIN = 5;
const uint8_t VALVE_2_PIN = 6;
const unsigned long PULSE_DURATION_MS = 40;

struct SortCommand {
    uint8_t valve;
    unsigned long targetTimeUs;
};

const uint8_t BUFFER_SIZE = 4;  // Must remain a power of 2
SortCommand cmdBuffer[BUFFER_SIZE];
uint8_t head = 0;
uint8_t tail = 0;

unsigned long valve1_off_time = 0;
unsigned long valve2_off_time = 0;
bool valve1_active = false;
bool valve2_active = false;

void setup() {
    Serial.begin(115200);
    Serial.setTimeout(10);  // Prevent parseInt() from blocking for 1s on malformed input
    pinMode(VALVE_1_PIN, OUTPUT);
    pinMode(VALVE_2_PIN, OUTPUT);
    digitalWrite(VALVE_1_PIN, LOW);
    digitalWrite(VALVE_2_PIN, LOW);
    Serial.println("MCU_READY");
}

void processIncomingSerial() {
    while (Serial.available() > 0) {
        if (Serial.read() != 'V') continue;

        int valveId = Serial.parseInt();
        if (Serial.read() != ',') continue;
        long delayUs = Serial.parseInt();

        // Consume through the newline terminator
        while (Serial.available() && Serial.peek() != '\n') Serial.read();
        if (Serial.available()) Serial.read();

        if (valveId < 1 || valveId > 2 || delayUs <= 0) continue;

        uint8_t nextHead = (head + 1) & (BUFFER_SIZE - 1);
        if (nextHead == tail) continue;  // Buffer full — drop packet

        cmdBuffer[head].valve = (uint8_t)valveId;
        cmdBuffer[head].targetTimeUs = micros() + (unsigned long)delayUs;
        head = nextHead;
    }
}

void checkAndExecuteSchedules() {
    if (tail == head) return;

    // Signed-cast subtraction is the correct way to compare micros() values across
    // the ~70-minute rollover boundary. When micros() wraps past 0 and targetTimeUs
    // hasn't fired yet, the difference goes negative as a signed long, keeping this
    // condition false until the real fire moment.
    if ((long)(micros() - cmdBuffer[tail].targetTimeUs) >= 0) {
        uint8_t valve = cmdBuffer[tail].valve;
        unsigned long now = millis();

        if (valve == 1) {
            digitalWrite(VALVE_1_PIN, HIGH);
            valve1_off_time = now + PULSE_DURATION_MS;
            valve1_active = true;
        } else if (valve == 2) {
            digitalWrite(VALVE_2_PIN, HIGH);
            valve2_off_time = now + PULSE_DURATION_MS;
            valve2_active = true;
        }

        tail = (tail + 1) & (BUFFER_SIZE - 1);
    }
}

void manageValveTimeout() {
    unsigned long now = millis();
    // Same signed-cast pattern guards against millis() rollover on pulse end times
    if (valve1_active && (long)(now - valve1_off_time) >= 0) {
        digitalWrite(VALVE_1_PIN, LOW);
        valve1_active = false;
    }
    if (valve2_active && (long)(now - valve2_off_time) >= 0) {
        digitalWrite(VALVE_2_PIN, LOW);
        valve2_active = false;
    }
}

void loop() {
    processIncomingSerial();
    checkAndExecuteSchedules();
    manageValveTimeout();
}
