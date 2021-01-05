#!/usr/bin/env python3
"""
Generate DNS records from my Ansible config.

This scripts looks at the following files:
- global-config/dns-entries.yml for custom DNS entries
- group_vars/all.yml for general AS settings; specifically the following options:
  "ownnets4", "ownnets6", "dns_*"
- The inventory file (hosts.yml) to create host records for routers, unless --no-host-records is set
-
"""

import argparse
import ipaddress
import math
import os
import sys

import jinja2

from _common import *

# Global state stuff
args = None
global_vars = {}
hosts = None

ptr_records = {}  # Mapping of IPs to PTR records
namedconf_entries = {} # Mapping of zones to their filenames

def get_zone_file(zonename):
    """
    Create a new zone file, and add it to the list of zones to be included in named.conf.
    Returns the file descriptor of the new zone file. The caller should close this file descriptor when finished with it.
    """
    fname = zonename.replace('/', '_') + '.zone'
    namedconf_entries[zonename] = fname
    local_path = os.path.join(args.out_dir, fname)
    fd = open(local_path, 'w')
    # FIXME: make these options configurable
    fd.write(f"""; Autogenerated by {sys.argv[0]}, do not edit!
$ORIGIN {zonename}
$TTL {global_vars['dns_ttl']}
@   IN  SOA     {global_vars['dns_nameserver_prefix']}.{global_vars['dns_domain']} placeholder-see-registry.{global_vars['dns_domain']} (
        1           ; serial
        7200        ; refresh period
        2400        ; retry period
        86400       ; expiration
        3600        ; minimum TTL
)
""")
    return fd

def _write_entry(fd, name, rtype, data, reverse_domain=None):
    """
    Write a DNS entry into the given file descriptor.
    If reverse_domain is given and rtype is either A or AAAA, also create a PTR record
    from data (the IP) to <name>.<reverse_domain>
    """
    rtype = rtype.upper()
    fd.write(f"{name} IN {rtype} {data}\n")
    if reverse_domain is not None:
        if rtype not in {'A', 'AAAA'}:
            raise ValueError("Cannot add PTR record: expected rtype A or AAAA but got %s" % rtype)
        else:
            ipaddr = ipaddress.ip_address(data)
            ptr_records[ipaddr] = f'{name}.{reverse_domain}'

def write_forward_zone(domain, records):
    """
    Write the data for a forward DNS zone.
    """
    print(f"Writing forward DNS zone for {domain}")
    fd = get_zone_file(domain)
    for record_name, data in records.items():
        if data['type'] == 'ansible_host_alias':
            hostdata = hosts[data['target']]
            _write_entry(fd, record_name, 'A',    hostdata['ownip'])
            _write_entry(fd, record_name, 'AAAA', hostdata['ownip6'])
        elif data['type'] == 'ansible_host_record':
            _write_entry(fd, record_name, 'A',    data['ip4'], reverse_domain=domain)
            _write_entry(fd, record_name, 'AAAA', data['ip6'], reverse_domain=domain)
        else:
            _write_entry(fd, record_name, data['type'], data['target'])
    # Add host records onto the main domain
    if domain == global_vars['dns_domain']:
        for router in hosts:
            _write_entry(fd, hosts[router]['shortname'], 'A',    hosts[router]['ownip'],  reverse_domain=domain)
            _write_entry(fd, hosts[router]['shortname'], 'AAAA', hosts[router]['ownip6'], reverse_domain=domain)
    fd.close()

def _write_ptr_zone(zonename, ipnet):
    print(f"Writing PTR zone {zonename} for {ipnet}")
    fd = get_zone_file(zonename)
    for ipaddr, record in ptr_records.items():
        if ipaddr in ipnet:
            if not record.endswith('.'):
                record += "."  # just to be sure
            _write_entry(fd, ipaddr.reverse_pointer, "PTR", record)
    fd.close()

def write_ptr4_zone(netblock):
    """
    Write a PTR zone for an IPv4 IP block.
    """
    ipnet = ipaddress.IPv4Network(netblock)
    # For IPv4 blocks that don't fit within a class boundary (/8, /16, /24) we want to use RFC2317 style
    # delegation, e.g. "112/28.229.20.172.in-addr.arpa" and create a delegation zone from the nearest classful block
    # For blocks that do fit on the class boundary, we can use the classic "3.2.1.in-addr.arpa" format as-is.
    if ipnet.prefixlen % 8 == 0:
        # IPv4Network.reverse_pointer will return things like "0/24.1.168.192.in-addr.arpa", but we don't want the leftmost octet
        zonename = ipnet.network_address.reverse_pointer.lstrip('0.')
        _write_ptr_zone(zonename, ipnet)
    else:
        zonename = ipnet.reverse_pointer
        fd = get_zone_file(zonename)
        # Calculate the closest classful block
        nearest_classful_prefixlen = math.floor(ipnet.prefixlen / 8) * 8
        assert ipnet.prefixlen > nearest_classful_prefixlen
        nearest_classful_net = ipnet.supernet(new_prefix=nearest_classful_prefixlen)

        classful_zonename = nearest_classful_net.network_address.reverse_pointer.lstrip('0.')
        print(f"Writing PTR zone {zonename} for {ipnet}")
        print(f"Writing RFC2317 PTR delegation zone {classful_zonename} for {ipnet}")
        classful_fd = get_zone_file(classful_zonename)
        for ipaddr in ipnet.hosts():
            if record := ptr_records.get(ipaddr):
                # Chop off the bits in common with the classful zone name
                relevant_octets = ipaddr.reverse_pointer[:len(ipaddr.reverse_pointer)-len(classful_zonename)-1]
                if not record.endswith('.'):
                    record += "."
                _write_entry(fd, relevant_octets, "PTR", record)
                # For the delegation zone we want records like this for 172.20.229.112/28:
                #   112 IN CNAME 112.112/28.229.20.172.in-addr.arpa
                #   113 IN CNAME 113.112/28.229.20.172.in-addr.arpa.
                # and so on
                cname_record = f'{relevant_octets}.{ipnet.reverse_pointer}.'
                _write_entry(classful_fd, relevant_octets, "CNAME", cname_record)
        classful_fd.close()
        fd.close()

def write_ptr6_zone(netblock):
    """
    Write a PTR zone for an IPv6 IP block.
    """
    ipnet = ipaddress.IPv6Network(netblock)
    # The reverse_pointer attribute on ipaddress.IPv6Network isn't really handled properly (it gives something like "8.4./.0.0.<other octets>.d.f.ip6.arpa")
    # IPv6Address deals with it better but still gives extra 0 octets, which we should strip off.
    # Final result is something like "7.b.1.1.d.a.b.0.6.8.d.f.ip6.arpa"
    zonename = ipnet.network_address.reverse_pointer.lstrip('0.')
    _write_ptr_zone(zonename, ipnet)

def write_named_conf():
    print("Writing named.conf")
    with open("global-config/named.conf", 'w') as f:
        f.write(f"""
options {{
    directory "{global_vars['dns_zones_dir']}";
}};
""")
        for zone, fname in namedconf_entries.items():
            f.write(f"""
zone "{zone}" {{
    file "{fname}";
}};
""")

def _load_config():
    global hosts
    hosts = yaml_load(args.hosts)['dn42routers']['hosts']
    group_vars = yaml_load(args.group_vars)

    # Follow Ansible templating for dns-entries.yml
    with open(args.dns_entries) as f:
        dns_entries_raw = f.read()
    dns_entries_tmpl = jinja2.Template(dns_entries_raw)
    dns_entries = yaml.full_load(dns_entries_tmpl.render(group_vars))

    global_vars.update(group_vars)
    global_vars.update(dns_entries)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--out-dir", help="output directory", default="global-config/dns-zones/")
    parser.add_argument("-H", "--hosts", help="path to hosts configuration / inventory file",
                        type=str, default='hosts.yml')
    parser.add_argument("-D", "--dns-entries", help="path to DNS entries configuration",
                        type=str, default='global-config/dns-entries.yml')
    parser.add_argument("-G", "--group-vars", help="path to group vars configuration",
                        type=str, default='group_vars/all.yml')
    global args
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    _load_config()

    # Write forward DNS zone
    for domain, records in global_vars['dns_records'].items():
        print(domain, records)
        write_forward_zone(domain, records)

    # Write PTR zones
    for netblock in global_vars['ownnets4']:
        write_ptr4_zone(netblock)
    for netblock in global_vars['ownnets6']:
        write_ptr6_zone(netblock)

    write_named_conf()

if __name__ == '__main__':
    main()
