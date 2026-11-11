/*
 * DUMMY EXAMPLE: the kind of C console program the Session Workflow replaces.
 *
 * It listens for one LED device, shows a menu and sends the chosen command
 * as a frame:
 *
 *     LEN | CMD | DATA... | CRC8
 *
 *   LEN   number of CMD + DATA bytes
 *   CRC8  CRC-8/SMBUS (poly 0x07, init 0x00) over LEN, CMD and DATA
 *
 * The device answers with CMD | 0x80 (0xFF + the command for an unknown one).
 * The protocol is invented for this example; see README.md for how each menu
 * entry becomes a workflow operation.  POSIX sockets; not built by the tests.
 *
 *     cc -o console_menu console_menu.c && ./console_menu 5000
 */
#include <arpa/inet.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

enum {
    CMD_PING      = 0x01,
    CMD_GET_INFO  = 0x02,
    CMD_LED_SET   = 0x10,   /* data: 0 = off, 1 = on */
    CMD_LED_BLINK = 0x11,   /* data: count, period in 10 ms units */
    CMD_GET_TEMP  = 0x20,   /* reply data: int16 big-endian, 0.1 degC */
    CMD_RESET     = 0x30,   /* no reply */
};

static uint8_t crc8(const uint8_t *data, size_t len)
{
    uint8_t crc = 0x00;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++)
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
    }
    return crc;
}

static int send_cmd(int sock, uint8_t cmd, const uint8_t *data, uint8_t len)
{
    uint8_t frame[260];
    frame[0] = (uint8_t)(len + 1);
    frame[1] = cmd;
    memcpy(&frame[2], data, len);
    frame[2 + len] = crc8(frame, 2u + len);
    return send(sock, frame, 3u + len, 0) == (ssize_t)(3u + len) ? 0 : -1;
}

static void print_reply(int sock)
{
    uint8_t buf[260];
    ssize_t n = recv(sock, buf, sizeof buf, 0);
    if (n <= 0) {
        printf("no reply\n");
        return;
    }
    printf("reply:");
    for (ssize_t i = 0; i < n; i++)
        printf(" %02X", buf[i]);
    printf("\n");
}

int main(int argc, char **argv)
{
    int port = argc > 1 ? atoi(argv[1]) : 5000;
    int server = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
    bind(server, (struct sockaddr *)&addr, sizeof addr);
    listen(server, 1);
    printf("waiting for the device on port %d...\n", port);
    int sock = accept(server, NULL, NULL);

    for (;;) {
        printf("\n1) Ping\n2) Send device info\n3) LED on\n4) LED off\n"
               "5) Blink LED 3 times\n6) Read temperature\n7) Reset\n0) Quit\n> ");
        int choice;
        if (scanf("%d", &choice) != 1 || choice == 0)
            break;
        uint8_t on = 1, off = 0, blink[2] = {3, 25};   /* 3 blinks, 250 ms */
        switch (choice) {
        case 1: send_cmd(sock, CMD_PING, NULL, 0); break;
        case 2: send_cmd(sock, CMD_GET_INFO, NULL, 0); break;
        case 3: send_cmd(sock, CMD_LED_SET, &on, 1); break;
        case 4: send_cmd(sock, CMD_LED_SET, &off, 1); break;
        case 5: send_cmd(sock, CMD_LED_BLINK, blink, 2); break;
        case 6: send_cmd(sock, CMD_GET_TEMP, NULL, 0); break;
        case 7: send_cmd(sock, CMD_RESET, NULL, 0); continue;   /* device does not answer */
        default: printf("unknown choice\n"); continue;
        }
        print_reply(sock);
    }
    close(sock);
    close(server);
    return 0;
}
