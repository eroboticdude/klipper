# Support for UC1701 (and similar) 128x64 graphics LCD displays
#
# Copyright (C) 2018-2019  Kevin O'Connor <kevin@koconnor.net>
# Copyright (C) 2018  Eric Callahan  <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from .. import bus
from . import font8x14

BACKGROUND_PRIORITY_CLOCK = 0x7fffffff00000000

TextGlyphs = { 'right_arrow': b'\x1a', 'degrees': b'\xf8' }

class DisplayBase:
    def __init__(self, io, columns=128, x_offset=0):
        self.send = io.send
        # framebuffers
        self.columns = columns
        self.x_offset = x_offset
        self.vram = [bytearray(self.columns) for i in range(8)]
        self.all_framebuffers = [(self.vram[i], bytearray(b'~'*self.columns), i)
                                 for i in range(8)]
        # Cache fonts and icons in display byte order
        self.font = [self._swizzle_bits(bytearray(c))
                     for c in font8x14.VGA_FONT]
        self.icons = {}
    def flush(self):
        # Find all differences in the framebuffers and send them to the chip
        for new_data, old_data, page in self.all_framebuffers:
            if new_data == old_data:
                continue
            # Find the position of all changed bytes in this framebuffer
            diffs = [[i, 1] for i, (n, o) in enumerate(zip(new_data, old_data))
                     if n != o]
            # Batch together changes that are close to each other
            for i in range(len(diffs)-2, -1, -1):
                pos, count = diffs[i]
                nextpos, nextcount = diffs[i+1]
                if pos + 5 >= nextpos and nextcount < 16:
                    diffs[i][1] = nextcount + (nextpos - pos)
                    del diffs[i+1]
            # Transmit changes
            for col_pos, count in diffs:
                # Set Position registers
                ra = 0xb0 | (page & 0x0F)
                ca_msb = 0x10 | ((col_pos >> 4) & 0x0F)
                ca_lsb = col_pos & 0x0F
                self.send([ra, ca_msb, ca_lsb])
                # Send Data
                self.send(new_data[col_pos:col_pos+count], is_data=True)
            old_data[:] = new_data
    def _swizzle_bits(self, data):
        # Convert from "rows of pixels" format to "columns of pixels"
        top = bot = 0
        for row in range(8):
            spaced = (data[row] * 0x8040201008040201) & 0x8080808080808080
            top |= spaced >> (7 - row)
            spaced = (data[row + 8] * 0x8040201008040201) & 0x8080808080808080
            bot |= spaced >> (7 - row)
        bits_top = [(top >> s) & 0xff for s in range(0, 64, 8)]
        bits_bot = [(bot >> s) & 0xff for s in range(0, 64, 8)]
        return (bytearray(bits_top), bytearray(bits_bot))
    def set_glyphs(self, glyphs):
        for glyph_name, glyph_data in glyphs.items():
            icon = glyph_data.get('icon16x16')
            if icon is not None:
                top1, bot1 = self._swizzle_bits(icon[0])
                top2, bot2 = self._swizzle_bits(icon[1])
                self.icons[glyph_name] = (top1 + top2, bot1 + bot2)
    def write_text(self, x, y, data):
        if x + len(data) > 16:
            data = data[:16 - min(x, 16)]
        pix_x = x * 8
        pix_x += self.x_offset
        page_top = self.vram[y * 2]
        page_bot = self.vram[y * 2 + 1]
        for c in bytearray(data):
            bits_top, bits_bot = self.font[c]
            page_top[pix_x:pix_x+8] = bits_top
            page_bot[pix_x:pix_x+8] = bits_bot
            pix_x += 8
    def write_graphics(self, x, y, data):
        if x >= 16 or y >= 4 or len(data) != 16:
            return
        bits_top, bits_bot = self._swizzle_bits(data)
        pix_x = x * 8
        pix_x += self.x_offset
        page_top = self.vram[y * 2]
        page_bot = self.vram[y * 2 + 1]
        for i in range(8):
            page_top[pix_x + i] ^= bits_top[i]
            page_bot[pix_x + i] ^= bits_bot[i]
    def write_glyph(self, x, y, glyph_name):
        icon = self.icons.get(glyph_name)
        if icon is not None and x < 15:
            # Draw icon in graphics mode
            pix_x = x * 8
            pix_x += self.x_offset
            page_idx = y * 2
            self.vram[page_idx][pix_x:pix_x+16] = icon[0]
            self.vram[page_idx + 1][pix_x:pix_x+16] = icon[1]
            return 2
        char = TextGlyphs.get(glyph_name)
        if char is not None:
            # Draw character
            self.write_text(x, y, char)
            return 1
        return 0
    def clear(self):
        zeros = bytearray(self.columns)
        for page in self.vram:
            page[:] = zeros
    def get_dimensions(self):
        return (16, 4)

# IO wrapper for "4 wire" spi bus (spi bus with an extra data/control line)
class SPI4wire:
    def __init__(self, config, data_pin_name):
        self.spi = bus.MCU_SPI_from_config(config, 0, default_speed=10000000)
        dc_pin = config.get(data_pin_name)
        self.mcu_dc = bus.MCU_bus_digital_out(self.spi.get_mcu(), dc_pin,
                                              self.spi.get_command_queue())
    def send(self, cmds, is_data=False):
        self.mcu_dc.update_digital_out(is_data,
                                       reqclock=BACKGROUND_PRIORITY_CLOCK)
        self.spi.spi_send(cmds, reqclock=BACKGROUND_PRIORITY_CLOCK)

# IO wrapper for i2c bus
class I2C:
    def __init__(self, config, default_addr):
        self.i2c = bus.MCU_I2C_from_config(config, default_addr=default_addr,
                                           default_speed=400000)
    def send(self, cmds, is_data=False):
        if is_data:
            hdr = 0x40
        else:
            hdr = 0x00
        cmds = bytearray(cmds)
        cmds.insert(0, hdr)
        self.i2c.i2c_write(cmds, reqclock=BACKGROUND_PRIORITY_CLOCK)

# Helper code for toggling a reset pin on startup
class ResetHelper:
    def __init__(self, pin_desc, io_bus):
        self.mcu_reset = None
        if pin_desc is None:
            return
        self.mcu_reset = bus.MCU_bus_digital_out(io_bus.get_mcu(), pin_desc,
                                                 io_bus.get_command_queue())
    def init(self):
        if self.mcu_reset is None:
            return
        mcu = self.mcu_reset.get_mcu()
        curtime = mcu.get_printer().get_reactor().monotonic()
        print_time = mcu.estimated_print_time(curtime)
        # Toggle reset
        minclock = mcu.print_time_to_clock(print_time + .100)
        self.mcu_reset.update_digital_out(0, minclock=minclock)
        minclock = mcu.print_time_to_clock(print_time + .200)
        self.mcu_reset.update_digital_out(1, minclock=minclock)
        # Force a delay to any subsequent commands on the command queue
        minclock = mcu.print_time_to_clock(print_time + .300)
        self.mcu_reset.update_digital_out(1, minclock=minclock)

# The UC1701 is a "4-wire" SPI display device
class UC1701(DisplayBase):
    def __init__(self, config):
        io = SPI4wire(config, "a0_pin")
        DisplayBase.__init__(self, io)
        self.contrast = config.getint('contrast', 40, minval=0, maxval=63)
        self.reset = ResetHelper(config.get("rst_pin", None), io.spi)
    def init(self):
        self.reset.init()
        init_cmds = [0xE2, # System reset
                     0x40, # Set display to start at line 0
                     0xA0, # Set SEG direction
                     0xC8, # Set COM Direction
                     0xA2, # Set Bias = 1/9
                     0x2C, # Boost ON
                     0x2E, # Voltage regulator on
                     0x2F, # Voltage follower on
                     0xF8, # Set booster ratio
                     0x00, # Booster ratio value (4x)
                     0x23, # Set resistor ratio (3)
                     0x81, # Set Electronic Volume
                     self.contrast, # Electronic Volume value
                     0xAC, # Set static indicator off
                     0x00, # NOP
                     0xA6, # Disable Inverse
                     0xAF] # Set display enable
        self.send(init_cmds)
        self.send([0xA5]) # display all
        self.send([0xA4]) # normal display
        self.flush()

# The SSD1306 supports both i2c and "4-wire" spi
class SSD1306(DisplayBase):
    def __init__(self, config, columns=128, x_offset=0):
        cs_pin = config.get("cs_pin", None)
        if cs_pin is None:
            io = I2C(config, 60)
            io_bus = io.i2c
        else:
            io = SPI4wire(config, "dc_pin")
            io_bus = io.spi
        self.reset = ResetHelper(config.get("reset_pin", None), io_bus)
        DisplayBase.__init__(self, io, columns, x_offset)
        self.contrast = config.getint('contrast', 239, minval=0, maxval=255)
        self.vcomh = config.getint('vcomh', 0, minval=0, maxval=63)
        self.invert = config.getboolean('invert', False)
    def init(self):
        self.reset.init()
        init_cmds = [
            0xAE,       # Display off
            0xD5, 0x80, # Set oscillator frequency
            0xA8, 0x3f, # Set multiplex ratio
            0xD3, 0x00, # Set display offset
            0x40,       # Set display start line
            0x8D, 0x14, # Charge pump setting
            0x20, 0x02, # Set Memory addressing mode
            0xA1,       # Set Segment re-map
            0xC8,       # Set COM output scan direction
            0xDA, 0x12, # Set COM pins hardware configuration
            0x81, self.contrast, # Set contrast control
            0xD9, 0xA1, # Set pre-charge period
            0xDB, self.vcomh, # Set VCOMH deselect level
            0x2E,       # Deactivate scroll
            0xA4,       # Output ram to display
            0xA7 if self.invert else 0xA6, # Set normal/invert
            0xAF,       # Display on
        ]
        self.send(init_cmds)
        self.flush()

# the SH1106 is SSD1306 compatible with up to 132 columns
class SH1106(SSD1306):
    def __init__(self, config):
        x_offset = config.getint('x_offset', 0, minval=0, maxval=3)
        SSD1306.__init__(self, config, 132, x_offset=x_offset)

#define ST_CMD_DELAY 0x80 # special signifier for command lists

#define ST7789_NOP     0x00
#define ST7789_SWRESET 0x01
#define ST7789_RDDID   0x04
#define ST7789_RDDST   0x09

#define ST7789_SLPIN   0x10
#define ST7789_SLPOUT  0x11
#define ST7789_PTLON   0x12
#define ST7789_NORON   0x13

#define ST7789_INVOFF  0x20
#define ST7789_INVON   0x21
#define ST7789_DISPOFF 0x28
#define ST7789_DISPON  0x29
#define ST7789_CASET   0x2A
#define ST7789_RASET   0x2B
#define ST7789_RAMWR   0x2C
#define ST7789_RAMRD   0x2E

#define ST7789_PTLAR   0x30
#define ST7789_COLMOD  0x3A
#define ST7789_MADCTL  0x36

  # Some ready-made 16-bit ('565') color settings:
#define ST77XX_BLACK 0x0000
#define ST77XX_WHITE 0xFFFF
#define ST77XX_RED 0xF800
#define ST77XX_GREEN 0x07E0
#define ST77XX_BLUE 0x001F
#define ST77XX_CYAN 0x07FF
#define ST77XX_MAGENTA 0xF81F
#define ST77XX_YELLOW 0xFFE0
#define ST77XX_ORANGE 0xFC00

class ST7789(DisplayBase): #for kobra 2 neo display
    
    def __init__(self, config, columns=128, x_offset=0): #display is 320x240, converting 1:2 to 3:4. might be stretched. Further looking into that can be done after this is tested
        io = SPI4wire(config, "dc_pin")
        io_bus = io.spi
        self.reset = ResetHelper(config.get("reset_pin", None), io_bus)
        DisplayBase.__init__(self, io, columns, x_offset)
        #self.contrast = config.getint('contrast', 239, minval=0, maxval=255)
        #self.vcomh = config.getint('vcomh', 0, minval=0, maxval=63)
        self.invert = config.getboolean('invert', False)

        NEW_WIDTH = 320
        NEW_HEIGHT = 240
        new_framebuffer = [[0] * NEW_WIDTH for _ in range(NEW_HEIGHT)]
        old_framebuffer = [[0] * NEW_WIDTH for _ in range(NEW_HEIGHT)]
        
    def set_address_window(self, x0, y0, x1, y1):
            self.send(0x2A) #CASET column address set command
            self.send([(x0 >> 8) & 0xFF, x0 & 0xFF, (x1 >> 8) & 0xFF, x1 & 0xFF])
    
            self.send(0x2B) #RASET row address set command
            self.send([(y0 >> 8) & 0xFF, y0 & 0xFF, (y1 >> 8) & 0xFF, y1 & 0xFF])

    def flush(self): #this is here to override the default inherited flush command and replace it with a version that converts the framebuffer of binary pixels to properly spaced RGB values for the ST7789   
            
        for new_data, old_data in self.all_framebuffers:
            
            def scale_framebuffer(new_data):
                for y in range(NEW_HEIGHT):
                    for x in range(NEW_WIDTH):
                        # Calculate corresponding coordinates in the old framebuffer
                        old_x = x * 128 // NEW_WIDTH  # Map x coordinate
                        old_y = y * 64 // NEW_HEIGHT  # Map y coordinate
                        
                        # Convert black-and-white to 16-bit RGB
                        pixel_value = new_data[old_y][old_x]
                        if pixel_value:  # Assuming non-zero means white
                            new_framebuffer[y][x] = 0xFFFF  # White in 16-bit RGB
                        else:
                            new_framebuffer[y][x] = 0x0000  # Black in 16-bit RGB
    
              # Convert old_data to the new framebuffer resolution
            scale_framebuffer(new_data)
        
                    # Find all differences in the framebuffers and send them to the chip
             for new_framebuffer, old_framebuffer in self.flush: 
                if new_framebuffer == old_framebuffer:
                    continue
        
                # Identify changed bytes
                diffs = [[i, 1] for i, (n, o) in enumerate(zip(new_framebuffer, old_framebuffer)) if n != o]
            
                # Batch nearby changes
                for i in range(len(diffs)-2, -1, -1):
                    pos, count = diffs[i]
                    nextpos, nextcount = diffs[i+1]
                    if pos + 5 >= nextpos and nextcount < 16:
                        diffs[i][1] = nextcount + (nextpos - pos)
                        del diffs[i+1]
                # Transmit changes
                for col_pos, count in diffs:

                    #set address window
                    x_start = col_pos
                    x_end = col_pos + count - 1
                    y_start = 0
                    y_end = 240
                  
                    set_address_window(x_start, y_start, x_end, y_end)
                    self.send(new_data[col_pos:col_pos+count], is_data=True)
        
            old_data[:] = new_data  # Update old_data for the next loop
            old_framebuffer[:] = new_framebuffer #update scaled framebuffer

            
    def init(self):
        self.reset.init()
        init_cmds = [
            0x3A, 0x55, #color mode 16bit
            0xB2, 0x0C, 0x0C, 0x00, 0x33, 0x33, #porch control
            0xB7, 0X35, #gate control, default value
            0xBB, 0x19, #VCOM setting 0.725v
            0xC0, 0x2C, #LCMCTRL Default
            0xC2, 0x01, #VDV and VRH command enable default 
            0xC3, 0x12, #VRH set +-4.45v
            0xC4, 0x20, #VDV set default
            0xC6, 0x0F, #frame rate 60hz
            0xD0, 0xA4, 0xA1, #power control default values
    
            0xE0, 0xD0, 0x04, 0x0D, 0x11, 0x13, 0x2B, 0x3F, 0x54, 0x4C, 0x18, 0x0D, 0x0B, 0x1F, 0x23, #positive voltage gamma control
            0xE1, 0xD0, 0x04, 0x0C, 0x11, 0x13, 0x2C, 0x3F, 0x44, 0x51, 0x2F, 0x1F, 0x1F, 0x20, 0x23, #negative voltage gamma control
    
            0x21 if self.invert else 0x20, # Set normal/invert
            0x11, #out of sleep mode
            0x13, #normal display on
            0x29, #display on        
        ]
        #reset display with 0x01
        self.send(init_cmds)
        self.flush()

