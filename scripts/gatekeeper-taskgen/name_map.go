package main

import "fmt"

type nameKey struct {
	kind      string
	namespace string
	name      string
}

type nameMap struct {
	entries map[nameKey]string
}

func newNameMap() *nameMap {
	return &nameMap{entries: map[nameKey]string{}}
}

func (nm *nameMap) set(kind, namespace, original, renamed string) {
	if original == "" {
		return
	}
	nm.entries[nameKey{kind: kind, namespace: namespace, name: original}] = renamed
}

func (nm *nameMap) mapName(kind, namespace, name string) string {
	if name == "" {
		return name
	}
	if renamed, ok := nm.entries[nameKey{kind: kind, namespace: namespace, name: name}]; ok {
		return renamed
	}
	return name
}

type nameAllocator struct {
	used map[nameKey]bool
}

func newNameAllocator() *nameAllocator {
	return &nameAllocator{used: map[nameKey]bool{}}
}

func (na *nameAllocator) allocate(kind, namespace, base string) (string, bool) {
	if base == "" {
		return "", false
	}
	key := nameKey{kind: kind, namespace: namespace, name: base}
	if !na.used[key] {
		na.used[key] = true
		return base, false
	}
	for i := 2; ; i++ {
		candidate := fmt.Sprintf("%s-%d", base, i)
		key = nameKey{kind: kind, namespace: namespace, name: candidate}
		if !na.used[key] {
			na.used[key] = true
			return candidate, true
		}
	}
}
