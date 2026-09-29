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
	flag.Parse()
	name := dns.Fqdn(*domain)
	key := &dns.DNSKEY{
		Hdr:   dns.RR_Header{Name: name, Rrtype: dns.TypeDNSKEY, Class: dns.ClassINET, Ttl: 300},
		Flags: dns.ZONE, Protocol: 3, Algorithm: dns.ED25519,
	}
	private, err := key.Generate(256)
	if err != nil {
		panic(err)
	}
	base := fmt.Sprintf("K%s+015+%05d", name, key.KeyTag())
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
