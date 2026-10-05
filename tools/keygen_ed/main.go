// keygen-ed writes a classical DNSSEC zone key in BIND format without needing BIND.
// It supports ED25519 (default), ECDSAP256SHA256 and RSASHA256 (2048 bits).
package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/miekg/dns"
)

func main() {
	domain := flag.String("domain", "cluster.local.", "signed DNS zone")
	out := flag.String("out", ".", "key directory")
	algorithm := flag.String("algorithm", "ED25519", "ED25519, ECDSAP256SHA256 or RSASHA256")
	flag.Parse()
	algorithms := map[string]struct {
		number uint8
		bits   int
	}{
		"ED25519":         {dns.ED25519, 256},
		"ECDSAP256SHA256": {dns.ECDSAP256SHA256, 256},
		"RSASHA256":       {dns.RSASHA256, 2048},
	}
	choice, ok := algorithms[*algorithm]
	if !ok {
		fmt.Fprintf(os.Stderr, "unsupported algorithm %q\n", *algorithm)
		os.Exit(2)
	}
	name := dns.Fqdn(*domain)
	key := &dns.DNSKEY{
		Hdr:   dns.RR_Header{Name: name, Rrtype: dns.TypeDNSKEY, Class: dns.ClassINET, Ttl: 300},
		Flags: dns.ZONE, Protocol: 3, Algorithm: choice.number,
	}
	private, err := key.Generate(choice.bits)
	if err != nil {
		panic(err)
	}
	base := fmt.Sprintf("K%s+%03d+%05d", name, choice.number, key.KeyTag())
	if err := os.MkdirAll(*out, 0700); err != nil {
		panic(err)
	}
	if err := os.WriteFile(filepath.Join(*out, base+".key"), []byte(key.String()+"\n"), 0644); err != nil {
		panic(err)
	}
	if err := os.WriteFile(filepath.Join(*out, base+".private"), []byte(key.PrivateKeyString(private)), 0600); err != nil {
		panic(err)
	}
	fmt.Println(base)
}
