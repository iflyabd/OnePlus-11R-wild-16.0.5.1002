#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
Make CONFIG_IP_SCTP ABI-neutral on Android/GKI vendor kernels.

Problem
-------
include/net/net_namespace.h declares the per-net SCTP state as a DIRECT member
of struct net:

    #if defined(CONFIG_IP_SCTP) || defined(CONFIG_IP_SCTP_MODULE)
            struct netns_sctp        sctp;
    #endif

Enabling SCTP (even =m) therefore changes sizeof(struct net).  genksyms then
assigns a new CRC to every exported symbol whose type mentions struct net, so a
freshly built sctp.ko is rejected by an already-running stock kernel:

    sctp: disagrees about version of symbol init_net
    finit_module() = -1 EINVAL

The same ABI break invalidates the stock vendor modules (cnss2, ipa, rmnet,
qcacld, ...) on a kernel that enables SCTP, because those .ko files were built
against a struct net without the member.

Fix
---
Move the per-net SCTP state into the net_generic cage -- the API that exists
for exactly this.  struct net keeps its stock layout, so every symbol CRC stays
identical to a stock kernel and sctp.ko loads into an unmodified, already
flashed kernel.  No flash, no vendor-module breakage.

    net->sctp.foo              ->  sctp_net(net)->foo
    sock_net(sk)->sctp.foo     ->  sctp_net(sock_net(sk))->foo
    asoc->base.net->sctp.foo   ->  sctp_net(asoc->base.net)->foo
    (net)->sctp.foo            ->  sctp_net(net)->foo      (stats macros)

Two spots cannot use net_generic directly and are handled explicitly:

  * net/sctp/protocol.c::sctp_addr_wq_timeout_handler() recovers struct net
    from the timer via from_timer(..., sctp.addr_wq_timer); with the member
    gone there is no such path, so struct netns_sctp gains a `struct net *net`
    back-pointer (private struct -- no ABI impact) set in sctp_defaults_init().

  * net/sctp/sysctl.c uses container_of(ctl->data, struct net, sctp.X) and
    rebases the per-net sysctl table by the address delta of &net->sctp.
    The handlers are switched to container_of(..., struct netns_sctp, X) and
    the table is rebased from a static address template instead.

Usage: patch-sctp-netns.py /path/to/kernel_platform/common
"""
import glob
import os
import re
import sys

K = sys.argv[1] if len(sys.argv) > 1 else "."
os.chdir(K)


def read(p):
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read()


def write(p, s):
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(s)


def die(msg):
    print("::error::patch-sctp-netns: " + msg)
    sys.exit(1)


def sub1(pattern, repl, text, what):
    """Regex-replace, requiring exactly one occurrence."""
    out, n = re.subn(pattern, repl, text)
    if n != 1:
        die("%s: expected 1 match, found %d" % (what, n))
    return out


changed = []

# --------------------------------------------------------------------- 1/7
# Drop the direct struct net member (the whole ABI break).
p = "include/net/net_namespace.h"
s = read(p)
s = sub1(
    r"[ \t]*#if defined\(CONFIG_IP_SCTP\)[^\n]*\n"
    r"[ \t]*struct netns_sctp[ \t]+sctp;\n"
    r"[ \t]*#endif\n",
    "",
    s,
    "net_namespace.h netns_sctp member",
)
write(p, s)
changed.append("%s: netns_sctp member removed from struct net" % p)

# --------------------------------------------------------------------- 2/7
# Private back-pointer inside struct netns_sctp, used only by the addr_wq
# timer handler (struct netns_sctp is no longer part of struct net, so this
# has no ABI impact).
p = "include/net/netns/sctp.h"
s = read(p)
if "struct net\t\t*net;" in s:
    die("netns/sctp.h already patched")
s = sub1(
    r"struct sock;\n",
    "struct sock;\nstruct net;\n",
    s,
    "netns/sctp.h forward declaration",
)
s = sub1(
    r"struct netns_sctp \{\n",
    "struct netns_sctp {\n"
    "\t/* Owning namespace; needed by code that only has the per-net\n"
    "\t * state (e.g. the addr_wq timer callback).  Private to SCTP.\n"
    "\t */\n"
    "\tstruct net\t\t*net;\n",
    s,
    "netns/sctp.h struct head",
)
write(p, s)
changed.append("%s: struct net *net back-pointer added" % p)

# --------------------------------------------------------------------- 3/7
# Give SCTP a net_generic id and let sctp_defaults_ops own the storage.
p = "net/sctp/protocol.c"
s = read(p)
if "sctp_net_id" in s:
    die("protocol.c already patched")
s = sub1(
    r"(static struct pernet_operations sctp_defaults_ops = \{\n)",
    r"unsigned int sctp_net_id __read_mostly;\n\n\1",
    s,
    "sctp_defaults_ops declaration",
)
s = sub1(
    r"(unsigned int sctp_net_id __read_mostly;\n\n"
    r"static struct pernet_operations sctp_defaults_ops = \{\n)",
    r"\1\t.id\t= &sctp_net_id,\n\t.size\t= sizeof(struct netns_sctp),\n",
    s,
    "sctp_defaults_ops .id/.size",
)
# Record the owning namespace at per-net init time (rewritten below into
# sctp_net(net)->net = net; by the generic rules).  It must go after the
# declaration block: the kernel builds with -Wdeclaration-after-statement.
s = sub1(
    r"(static int __net_init sctp_defaults_init\(struct net \*net\)\n"
    r"\{\n"
    r"\tint status;\n)",
    r"\1\n\tnet->sctp.net = net;\n",
    s,
    "sctp_defaults_init back-pointer init",
)
# The addr_wq timer callback cannot use from_timer(net, t, sctp.addr_wq_timer)
# any more: reach the namespace through the back-pointer instead.
s = sub1(
    r"[ \t]*struct net \*net = from_timer\(net, t, sctp\.addr_wq_timer\);\n",
    "\tstruct netns_sctp *sctp = from_timer(sctp, t, addr_wq_timer);\n"
    "\tstruct net *net = sctp->net;\n",
    s,
    "sctp_addr_wq_timeout_handler from_timer",
)
write(p, s)
changed.append("%s: sctp_net_id + .id/.size, back-pointer init, timer fix" % p)

# --------------------------------------------------------------------- 4/7
# Accessor for the net/sctp translation units, plus the three SNMP stat
# macros that dereference (net)->sctp.
p = "include/net/sctp/sctp.h"
s = read(p)
if "sctp_net(" in s:
    die("include/net/sctp/sctp.h already patched")
s = sub1(
    r"#include <net/sctp/constants.h>\n",
    "#include <net/sctp/constants.h>\n"
    "#include <net/net_namespace.h>\n"
    "#include <net/netns/generic.h>\n"
    "#include <net/netns/sctp.h>\n"
    "\n"
    "/*\n"
    " * ABI-neutral per-net SCTP state.\n"
    " *\n"
    " * struct netns_sctp is reached through the net_generic cage instead of\n"
    " * being embedded in struct net, so that enabling CONFIG_IP_SCTP does not\n"
    " * change sizeof(struct net) and therefore keeps the kernel ABI (and the\n"
    " * symbol CRCs expected by prebuilt vendor modules) stable.\n"
    " */\n"
    "extern unsigned int sctp_net_id;\n"
    "\n"
    "static inline struct netns_sctp *sctp_net(const struct net *net)\n"
    "{\n"
    "\treturn net_generic(net, sctp_net_id);\n"
    "}\n",
    s,
    "include/net/sctp/sctp.h accessor",
)
s = s.replace("(net)->sctp.sctp_statistics", "sctp_net(net)->sctp_statistics")
if "->sctp." in s:
    die("include/net/sctp/sctp.h: unrewritten ->sctp. reference remains")
write(p, s)
changed.append("%s: sctp_net() accessor, stat macros rewritten" % p)

# --------------------------------------------------------------------- 5/7
# net/sctp/sysctl.c: container_of() and the per-net table rebase.
p = "net/sctp/sysctl.c"
s = read(p)
marker = "int sctp_sysctl_net_register(struct net *net)"
if marker not in s:
    die("sysctl.c: sctp_sysctl_net_register not found")
idx = s.index(marker)
head, tail = s[:idx], s[idx:]

# 5a. handlers: the per-net state is now its own object
head, n = re.subn(
    r"container_of\(\s*ctl->data\s*,\s*struct net\s*,\s*sctp\.(\w+)\s*\)",
    r"container_of(ctl->data, struct netns_sctp, \1)",
    head,
)
if n != 4:
    die("sysctl.c: expected 4 container_of conversions, found %d" % n)
head, n = re.subn(
    r"struct net \*net = container_of\(ctl->data, struct netns_sctp,",
    "struct netns_sctp *sctp = container_of(ctl->data, struct netns_sctp,",
    head,
)
if n != 4:
    die("sysctl.c: expected 4 handler locals renamed, found %d" % n)
head = head.replace("net->sctp.", "sctp->")

# 5b. per-net table: static address template + rebase in the register path
head = sub1(
    r"(static struct ctl_table sctp_net_table\[\] = \{\n)",
    "/*\n"
    " * Address template for the per-net sysctl table.  The real per-net state\n"
    " * lives in net_generic(); sctp_sysctl_net_register() rebases every ->data\n"
    " * pointer from this template onto the target net's struct netns_sctp.\n"
    " */\n"
    "static struct netns_sctp sctp_net_template;\n"
    "\n"
    r"\1",
    head,
    "sysctl.c template declaration",
)
head = head.replace("&init_net.sctp.", "&sctp_net_template.")

tail = sub1(
    r"table\[i\]\.data \+= \(char \*\)\(&net->sctp\) - \(char \*\)&init_net\.sctp;",
    "table[i].data += (char *)sctp_net(net) - (char *)&sctp_net_template;",
    tail,
    "sysctl.c table rebase",
)
tail = tail.replace("net->sctp.", "sctp_net(net)->")
s = head + tail
if "init_net.sctp" in s or "struct net, sctp." in s:
    die("sysctl.c: stale references remain")
write(p, s)
changed.append("%s: container_of + table rebase converted" % p)

# --------------------------------------------------------------------- 6/7
# Generic dereference rewrites for every remaining net/sctp source file.
GEN = [
    (r"sock_net\(\s*sk\s*\)->sctp\.", "sctp_net(sock_net(sk))->"),
    (r"asoc->base\.net->sctp\.", "sctp_net(asoc->base.net)->"),
    (r"\(net\)->sctp\.", "sctp_net(net)->"),
    (r"\bnet->sctp\.", "sctp_net(net)->"),
]

rewritten = 0
for f in sorted(glob.glob("net/sctp/*.c")):
    if os.path.basename(f) == "sysctl.c":
        continue
    s = read(f)
    orig = s
    for pat, rep in GEN:
        s = re.sub(pat, rep.replace("\\", "\\\\"), s)
    if s != orig:
        write(f, s)
        rewritten += 1
        changed.append("%s: %d sctp_net() sites" % (f, s.count("sctp_net(")))
print("patch-sctp-netns: generic rewrites in %d files" % rewritten)

# --------------------------------------------------------------------- 7/7
# Verify: no dereference of the removed member survives anywhere.
leftovers = []
for f in sorted(glob.glob("net/sctp/*.c")) + ["include/net/sctp/sctp.h",
                                              "net/sctp/sctp.h"]:
    if not os.path.exists(f):
        continue
    for i, line in enumerate(read(f).splitlines(), 1):
        if "->sctp." in line:
            leftovers.append("%s:%d: %s" % (f, i, line.strip()))
if leftovers:
    print("\n".join(leftovers))
    die("%d unrewritten ->sctp. references remain" % len(leftovers))

nn = read("include/net/net_namespace.h")
if re.search(r"struct netns_sctp\s+sctp;", nn):
    die("struct net still declares netns_sctp")
if "struct netns_sctp" in nn.replace("#include <net/netns/sctp.h>", ""):
    die("unexpected remaining struct netns_sctp reference in net_namespace.h")

print("patch-sctp-netns: ABI-neutral SCTP applied")
for c in changed:
    print("  " + c)
