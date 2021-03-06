#!/usr/bin/env python

import re
import time
import sys
from subprocess import Popen, PIPE
from optparse import OptionParser, OptionGroup, TitledHelpFormatter

def sizeof_fmt(num, base=1024.0):
    for x in ['','K','M','G','T']:
        if num < base:
            return "%3.1f%s" % (num, x)
        num /= base

class TcParserException(Exception): pass

class TcParser(object):
    def __init__(self, interface, sleep_time):
        self.interface = interface
        self.sleep_time = sleep_time

    def get_filters(self):
        data = []
        p = Popen(["tc", "filter", "show", "dev", self.interface], stdout=PIPE)
        stdout, stderr = p.communicate()
        p.wait()
        buf = []
        last = ""
        for line in stdout.split("\n"):
            if ("match" in last and not "match" in line) or ("match" not in last and "match" not in line):
                data.append("\n".join(buf))
                buf = []
            buf.append(line)
            last = line
        return data

    def parse_filters(self):
        parsed = []
        data = self.get_filters()
        for item in data:
            parsed_item = {}
            res = re.search("flowid ([0-9\:]*)", item, re.S)
            if res and len(res.groups()) == 1:
                parsed_item["class_id"] = res.groups()[0]
            res = re.findall("match ([a-f0-9\/]*)", item, re.S)
            if len(res) == 1:
                ip_a = int(res[0][0:2], 16)
                ip_b = int(res[0][2:4], 16)
                ip_c = int(res[0][4:6], 16)
                ip_d = int(res[0][6:8], 16)
                parsed_item["ipaddr"] = "%d.%d.%d.%d" % (ip_a, ip_b, ip_c, ip_d)
            elif len(res) == 2:
                ip_a = res[0][0:4]
                ip_b = res[0][4:8]
                ip_c = res[1][0:4]
                ip_d = res[1][4:8]
                parsed_item["ipaddr"] = "%s:%s:%s:%s::" % (ip_a, ip_b, ip_c, ip_d)
            else: continue
            parsed.append(parsed_item)
        return parsed

    def get_classes(self):
        data = []
        p = Popen(["tc", "-s", "class", "show", "dev", self.interface], stdout=PIPE)
        stdout, stderr = p.communicate()
        p.wait()
        buf = []
        for line in stdout.split("\n"):
            if not line:
                data.append("\n".join(buf))
                buf = []
            else:
                buf.append(line)
        return data
        
    def parse_classes(self):
        filters = self.parse_filters()
        parsed = []
        data = self.get_classes()
        for item in data:
            parsed_item = {}
            res = re.search("class [htbfscw]* ([0-9\:]*) parent ([0-9\:]*)", item, re.S)
            if not res:
                res = re.search("class [htbfscw]* ([0-9\:]*)", item, re.S)

            if res and len(res.groups()) == 2:
                parsed_item["class_id"] = res.groups()[0]
                parsed_item["parent"] = res.groups()[1]
            elif res and len(res.groups()) == 1:
                parsed_item["class_id"] = res.groups()[0]
                parsed_item["parent"] = None
            else:
                continue
            res = re.findall("([0-9MK]*)bit", item, re.S)
            if res:
                max_speed = max([int(x.replace("K", "000").replace("M", "000000")) for x in res])
                if max_speed:
                    parsed_item["max"] = max_speed
            res = re.search("Sent ([0-9]*) bytes ([0-9]*) pkt \(dropped ([0-9]*), overlimits ([0-9]*) requeues ([0-9]*)\)", item, re.S)
            if res and len(res.groups()) == 5:
                parsed_item["stat_sent_bytes"] = int(res.groups()[0])
                parsed_item["stat_sent_packets"] = int(res.groups()[1])
                parsed_item["stat_dropped"] = int(res.groups()[2])
                parsed_item["stat_overlimits"] = int(res.groups()[3])
                parsed_item["stat_requeues"] = int(res.groups()[4])
            else:
                continue
            tmp = filter(lambda x: x["class_id"] == parsed_item["class_id"], filters)
            if tmp:
                parsed_item["ipaddr"] = tmp[0]["ipaddr"]
            else:
                parsed_item["ipaddr"] = "0.0.0.0"
            if parsed_item:
                parsed.append(parsed_item)
        return parsed

    def get_rates(self):
        t1 = time.time()
        data1 = self.parse_classes()
        time.sleep(self.sleep_time)
        t2 = time.time()
        data2 = self.parse_classes()

        for item2 in data2:
            tmp = filter(lambda x: x["class_id"] == item2["class_id"], data1)
            if tmp:
                item1 = tmp[0]
                item2["rate_bytes"] = (item2["stat_sent_bytes"] - item1["stat_sent_bytes"]) / (t2 - t1)
                item2["rate_packets"] = (item2["stat_sent_packets"] - item1["stat_sent_packets"]) / (t2 - t1)

        return data2

    def sort_data(self, order_by="rate_packets"):
        values = (
            "stat_sent_bytes",
            "stat_sent_packets",
            "stat_dropped",
            "stat_overlimits",
            "stat_requeues",
            "rate_bytes",
            "rate_packets",
        )
        if order_by not in values:
            raise TcParserException("I can sort just by this values %s" % ", ".join(values))
        if order_by in ("rate_bytes", "rate_packets"):
            data = self.get_rates()
        else:
            data = self.parse_classes()
        data = sorted(data, key=lambda x: x[order_by])
        return data

    def get_tree(self, data=[]):
        data = data if data else self.parse_classes()
        tree = {}
        roots = filter(lambda x: not x["parent"], data)

        def arm(parent):
            arm_data = {}
            for cls in [x["class_id"] for x in data if x["parent"] == parent]:
                arm_data[cls] = arm(cls)
            return arm_data

        for root in roots:
            tree[root["class_id"]] = arm(root["class_id"])
        return tree

def row_head(indent=1):
    buf = "Tree".ljust(4 + indent * 4)
    buf += "IP".ljust(25)
    buf += "Rate".ljust(12)
    buf += "Max rate".ljust(12)
    buf += "Rate%".ljust(7)
    buf += "Total%".ljust(7)
    buf += "Packets".ljust(7)
    return buf
    

def row_format(cls, ip, rate, rate_packets, max_rate, total_max_rate=0, data_indent=1):
    buf = ""
    buf += cls.ljust(4 + data_indent * 4)
    buf += ip.ljust(25)
    buf += (sizeof_fmt(rate*8) + "bps").ljust(12)
    buf += (sizeof_fmt(max_rate) + "bps").ljust(12)
    buf += ("%.0f %%" % ((rate * 8) / max_rate * 100)).ljust(7)
    if total_max_rate: buf += ("%.0f %%" % (rate / total_max_rate)).ljust(7)
    else: buf += "--- %".ljust(7)
    buf += ("%d" % rate_packets).ljust(8)
    return buf

def main():
    #for x in tc_class():
    #   print x
    parser = OptionParser()
    parser.add_option("-t", "--tree", dest="tree", help="Network interface", action="store_true")
    parser.add_option("-i", "--interface", dest="interface", help="Network interface", metavar="INTERFACE")
    parser.add_option("-I", "--interval", dest="interval", help="Counting interval", metavar="INTERVAL")
    parser.add_option("-n", "--num", dest="num", help="Number of lines", metavar="NUM")

    (options, args) = parser.parse_args()

    max_speed = 200000000.0/8

    if options.interval:
        sleep_time = int(options.interval)
    else:
        sleep_time = 3
    if options.num:
        num = int(options.num)
    else:
        num = 20
    if options.interface and not options.tree:
        tcparser = TcParser(options.interface, sleep_time)
        data = tcparser.sort_data("rate_bytes")
        data.reverse()
        i = 1

        bufs = []
        total_bytes = 0.0
        total_bits = 0.0
        total_packets = 0

        head = "num.".ljust(5)
        head += "cls_id".ljust(8)
        head += "IP".ljust(16)
        head += "rate (bytes)".ljust(12)
        head += "rate (bits)".ljust(12)
        head += "max".ljust(12)
        head += "max %".ljust(7)
        head += "rate %".ljust(7)
        head += "load %".ljust(7)
        head += "pkts".ljust(8)
        head += "pkts %".ljust(7)

        for x in data:
            total_bytes += x["rate_bytes"]
            total_bits += x["rate_bytes"]*8
            total_packets += x["rate_packets"]

        if len(data) > num and num != -1:
            data = data[0:num]
        for x in data:
            buf = ("%d." % i).ljust(5)
            buf += x["class_id"].ljust(8)
            buf += x["ipaddr"].ljust(16)
            buf += (sizeof_fmt(x["rate_bytes"]) + "Bps").ljust(12)
            buf += (sizeof_fmt(x["rate_bytes"]*8) + "bps").ljust(12)
            buf += (sizeof_fmt(x["max"]) + "bps").ljust(12)
            buf += ("%.0f %%" % ((x["rate_bytes"] * 8) / x["max"] * 100)).ljust(7)
            if total_bytes: buf += ("%.0f %%" % (x["rate_bytes"] / total_bytes * 100)).ljust(7)
            else: buf += "--- %".ljust(7)
            if max_speed: buf += ("%.0f %%" % (x["rate_bytes"] / max_speed * 100)).ljust(7)
            else: buf += "--- %".ljust(7)
            buf += ("%d" % x["rate_packets"]).ljust(8)
            if total_packets: buf += ("%.0f %%" % (x["rate_packets"] / total_packets * 100)).ljust(7)
            else: buf += "--- %".ljust(7)
            bufs.append(buf)
            i += 1

        print "Total bytes: %sBps | Total bits: %sbps | Total packets: %d pkts" % (sizeof_fmt(total_bytes), sizeof_fmt(total_bits), total_packets)
        print "Total bytes (load): %.0f %%" % (float(total_bytes) / max_speed * 100)
        print 
        print head
        print "----------------------------------------------------------------------------------------------------"
        print "\n".join(bufs)
        sys.exit(0)

    elif options.interface and options.tree:
        tcparser = TcParser(options.interface, sleep_time)
        data = tcparser.sort_data("rate_bytes")
        arms = tcparser.get_tree()

        def get_lenghts(clss, nested=0):
            lenghts = []
            s = ""
            for cls in clss:
                lenghts.append(nested+1)
                lenghts += get_lenghts(clss[cls], nested + 1)
            return lenghts
        lenghts = get_lenghts(arms)
        max_indent = max(lenghts) if lenghts else 0

        def get_row(cls, indent=0):
            filtered = filter(lambda x: x["class_id"] == cls, data)
            if len(filtered) == 1:
                item = filtered[0]
                s = " " * indent
                if "ipaddr" in item and "rate_bytes" in item and "rate_packets" in item and "max" in item:
                    s += row_format(cls, item["ipaddr"], item["rate_bytes"], item["rate_packets"], item["max"], 200000, data_indent = max_indent - indent / 4)
                else:
                    s += cls
                s += "\n"
            else:
                s += "Parse error: %s - %s" % (cls, str(filtered))
                s += "\n"
            return s
        order_by = "rate_bytes"
        def print_arm(clss, indent=0):
            s = ""
            subdata = sorted(filter(lambda x: x["class_id"] in clss, data), key=lambda x: x[order_by])
            subdata.reverse()
            for item in subdata:
                item["clss"] = clss[filter(lambda x: x == item["class_id"], clss)[0]]
            for item in subdata:
                s += get_row(item["class_id"], indent)
                s += print_arm(item["clss"], indent + 4)
            #for cls in subdata:
            #    s += get_row(cls, indent)
            #    s += print_arm(clss[cls], indent + 4)
            return s
        print row_head(max_indent)
        print print_arm(arms, 0)

        sys.exit(0)
    parser.print_help()

if __name__ == "__main__":
    main()


