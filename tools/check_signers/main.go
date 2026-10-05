// check_signers checks every final-campaign signer through the pinned DNS fork.
package main

import (
	"crypto"
	"encoding/base64"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/miekg/dns"
	"github.com/open-quantum-safe/liboqs-go/oqs"
)

func check(name string, id uint8, keygen string) error {
	key := &dns.DNSKEY{Hdr: dns.RR_Header{Name: "cluster.local.", Rrtype: dns.TypeDNSKEY, Class: dns.ClassINET, Ttl: 300}, Flags: dns.ZONE, Protocol: 3, Algorithm: id}
	var private []byte
	var classical crypto.Signer
	if id == 8 || id == 13 || id == 15 {
		dir, err := os.MkdirTemp("", "dns-classical-check-")
		if err != nil {
			return err
		}
		defer os.RemoveAll(dir)
		out, err := exec.Command(keygen, "-algorithm", name, "-domain", "cluster.local.", "-out", dir).CombinedOutput()
		if err != nil {
			return fmt.Errorf("keygen: %w: %s", err, out)
		}
		base := filepath.Join(dir, strings.TrimSpace(string(out)))
		pub, err := os.ReadFile(base + ".key")
		if err != nil {
			return err
		}
		rr, err := dns.NewRR(string(pub))
		if err != nil {
			return err
		}
		key = rr.(*dns.DNSKEY)
		if key.Algorithm != id {
			return fmt.Errorf("wrong key algorithm: %d", key.Algorithm)
		}
		secret, err := os.Open(base + ".private")
		if err != nil {
			return err
		}
		defer secret.Close()
		parsed, err := key.ReadPrivateKey(secret, base+".private")
		if err != nil {
			return err
		}
		var ok bool
		classical, ok = parsed.(crypto.Signer)
		if !ok {
			return fmt.Errorf("private key is not a signer")
		}
	} else {
		var signer oqs.Signature
		if err := signer.Init(name, nil); err != nil {
			return err
		}
		defer signer.Clean()
		pub, err := signer.GenerateKeyPair()
		if err != nil {
			return err
		}
		private = append([]byte(nil), signer.ExportSecretKey()...)
		key.PublicKey = base64.StdEncoding.EncodeToString(pub)
	}
	for i := 0; i < 2; i++ {
		rrset := []dns.RR{&dns.A{Hdr: dns.RR_Header{Name: "svc.cluster.local.", Rrtype: dns.TypeA, Class: dns.ClassINET, Ttl: 5}, A: net.IPv4(10, 250, 0, byte(i+1))}}
		now := uint32(time.Now().Unix())
		sig := &dns.RRSIG{Hdr: dns.RR_Header{Rrtype: dns.TypeRRSIG, Class: dns.ClassINET, Ttl: 5}, Algorithm: id, KeyTag: key.KeyTag(), SignerName: key.Hdr.Name, Inception: now - 60, Expiration: now + 3600}
		var err error
		if classical != nil {
			err = sig.Sign(classical, rrset)
		} else {
			err = sig.SignWithPQC(nil, rrset, private)
		}
		if err != nil {
			return err
		}
		if err = sig.Verify(key, rrset); err != nil {
			return err
		}
		bad := []dns.RR{dns.Copy(rrset[0])}
		bad[0].(*dns.A).A = net.IPv4(10, 250, 0, 99)
		if sig.Verify(key, bad) == nil {
			return fmt.Errorf("tampered record accepted")
		}
	}
	return nil
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: check-signers PATH_TO_KEYGEN_ED")
		os.Exit(2)
	}
	for _, c := range []struct {
		name string
		id   uint8
	}{
		{"ED25519", 15}, {"ECDSAP256SHA256", 13}, {"RSASHA256", 8},
		{"Falcon-512", 17}, {"Falcon-1024", 27}, {"ML-DSA-44", 18}, {"ML-DSA-65", 28}, {"ML-DSA-87", 38},
		{"SPHINCS+-SHA2-128s-simple", 19}, {"MAYO-1", 20}, {"MAYO-3", 30}, {"SNOVA_24_5_4", 21},
	} {
		if err := check(c.name, c.id, os.Args[1]); err != nil {
			fmt.Fprintf(os.Stderr, "%s: %v\n", c.name, err)
			os.Exit(1)
		}
		fmt.Printf("%s: key/sign/verify/tamper checks passed\n", c.name)
	}
}
